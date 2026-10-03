# Changelog

All notable changes to Muninn are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Search-path hardening. The search path could wedge completely while `/health/*`
and `/scrape` stayed fast: `queue_depth` froze, every `/search` returned `504`
after the caller's own 120 s read timeout, and both `circuit_breaker_trips` and
`queue_rejections` stayed at zero, so nothing reported a fault.

### Fixed

- **The queue stopped draining, permanently.** A caller that gave up at its own
  read timeout cancelled the job future (`wait_for` cancels what it awaits), and
  the worker later died resolving it: `set_result` on a cancelled future raised
  `InvalidStateError`, the `except Exception` around it raised `InvalidStateError`
  again on the same future, and that second one propagated out of the only worker
  in the process. Nothing supervised it, so from then on nothing drained the
  queue and every query timed out at 120 s while the health endpoints kept
  answering in 80 ms. The job future is now awaited through a shield, settlement
  can never raise into a worker, and workers are supervised.
- **One hung upstream call could pin a worker indefinitely.** Every job now runs
  under a hard deadline (`SEARCH_JOB_DEADLINE_SECONDS`, default 45 s) covering
  every engine attempt and retry. On expiry the job is aborted, the engine in
  flight is charged one failure, and the worker is released.
- **Histogram bucket series were published under the wrong metric name.** With
  more than one histogram in the registry, every `*_bucket` line was rendered
  under whichever name happened to be bound last, so per-queue and per-engine
  latencies were mislabelled in `/metrics`.
- **`muninn_search_queue_wait_seconds` was declared and never recorded**, so the
  histogram that answers "why is search slow" never appeared in the exposition.
  A test now asserts that every declared search metric is actually emitted.
- **Engine quarantines escalated to 12 hours.** With a four-engine pool that
  turns three failures into a one-engine pool for most of a day. Cooldowns are
  now exponential per consecutive failure and capped at 30 minutes
  (`QUARANTINE_ESCALATED_SECONDS`), from a 5-minute base
  (`QUARANTINE_FIRST_SECONDS`), so engines are re-probed often and capacity
  survives.
- **A test needed a downloaded Chromium.** `test_docs_can_be_disabled` built a
  real `BrowserDriver` instead of injecting the fake one every other test uses,
  so it could only pass where `make browsers` had been run.

### Added

- **A search load breaker** (`app/search_breaker.py`) that refuses new search work
  in milliseconds when the queue is full or the workers are not draining,
  answering `503` with a machine-readable `reason`, `queue_depth` and
  `worker_state`, plus `Retry-After`. After refusing for
  `SEARCH_BREAKER_OPEN_SECONDS` it goes half-open and admits exactly one probe
  request: a completed job closes it, a failed or timed-out job re-opens it with a
  longer backoff, so it recovers without a restart instead of latching.
- **Worker supervision.** A worker that dies is replaced, and the queue entries it
  left behind are failed rather than left to rot. Worker identity, state,
  in-flight job age, jobs completed, jobs killed and restarts are on
  `/health/live`, `/health` and `/status`.
- **Engine failure classes.** A CAPTCHA/429, a timeout, a network error and a
  schema change are classified separately (`block`, `timeout`, `network`,
  `parse`), weighted differently when the cooldown is computed, and reported as
  `last_failure_class` in `/status`. Repeated failures with zero successes log a
  warning, because a longer cooldown cannot fix an engine that never answered.
- **Concurrency isolation.** A small worker pool (`SEARCH_WORKER_COUNT`, default
  2) with a per-engine concurrency cap
  (`SEARCH_MAX_CONCURRENT_PER_ENGINE`), so a hanging engine occupies one worker
  and cannot consume the capacity of the healthy ones.
- **Abandoned requests stop consuming a worker.** `/search` polls for a client
  disconnect while waiting and abandons the job; the deadline remains the
  guarantee, not this.
- **Search observability:** `muninn_search_job_seconds{outcome}` and
  `muninn_search_jobs_total{outcome}`, `muninn_search_deadline_kills_total{engine}`,
  `muninn_engine_total{engine,outcome}`, `muninn_search_breaker_state`,
  `muninn_search_breaker_trips_total{reason}`,
  `muninn_search_breaker_rejections_total{reason}`, `muninn_search_worker_state`,
  `muninn_search_workers`, `muninn_search_worker_restarts_total` and
  `muninn_search_queue_stuck`.
- **Alert rules** in `ops/prometheus_alerts.yml`: a queue that is not draining, a
  breaker that stays open, a worker that keeps dying, a run of deadline kills, and
  an engine failing without ever succeeding.
- **Acceptance tests** for all six criteria in `tests/test_search_resilience.py`,
  including a replay of the incident itself.

### Fixed

Four defects found in post-hardening testing of the search path. All four trace
back to one thing: the throttle wait sat between the dequeue and the job, outside
everything the service measured, so a healthy throttle-bound queue was
indistinguishable from a dead dispatcher — and a queue nothing was measuring
could not be bounded, refused, or released.

- **Workers reported `idle` while the queue was full.** The throttle sleep
  (15-30 s) happened after `queue.get()` but before the job began, so a worker
  waiting its turn looked idle with a job in hand. With the throttle longer than
  a fetch, almost every sample showed two idle workers over a queue of five. The
  throttle is now part of the job, and a worker in it reports `throttled`.
- **The job deadline did not cover the queue wait**, so a job that never got
  dispatched was never killed and its caller simply waited. A second deadline
  (`SEARCH_QUEUE_WAIT_DEADLINE_SECONDS`) bounds the time from enqueue to
  starting, throttle included; expiry is a fast 503, because nothing ran.
- **The breaker could not see this.** It only learned from jobs that failed after
  executing, so a queue that drained slowly but successfully never tripped it.
  "Queue not empty, every worker idle" is now a checked condition
  (`no_worker_picking_up`) that opens the breaker and refuses new work
  immediately. It is only checkable because the throttle reports itself.
- **`jobs_abandoned` could never increment.** Client-disconnect detection was
  silently dead: `Request.is_disconnected()` never returns True when the app
  wraps requests in Starlette's `BaseHTTPMiddleware`, which is what
  `@app.middleware("http")` builds and what the request-id middleware was. Five
  socket-level hang-ups produced zero detections across ~50 polls each. The
  request context is now pure ASGI middleware, which passes `receive` through
  untouched; hang-ups are detected within one poll and the worker is released
  immediately instead of after the job deadline.

### Changed

- **Pinning a quarantined engine is refused immediately** with
  `503 {"error": "engine_quarantined", "engine": "ddg"}` instead of being
  silently rotated to another engine, which was useless for a caller pinning an
  engine for a `site:` query.
- **`503` and `504` now mean different things.** `503` is "I cannot serve this
  now" (breaker open, queue saturated, engine quarantined); `504` is reserved for
  a job that actually ran and failed, including a job killed at its deadline.
  `200`/`422`/`429` are unchanged, and cached answers (`cached: true`) stay free
  and are never refused by a saturated search path.
- **Readiness ignores the load breaker, deliberately.** An instance pulled out of
  rotation while it refuses work would never receive the probe that closes the
  breaker, which is exactly how a breaker latches forever.
- The service warns at startup when the throttle plus the job deadline can exceed
  `REQUEST_TIMEOUT_SECONDS`, because callers give up before the worker does.

## [0.1.0] - 2026-09-26

First public release: a self-hosted search gateway with an isolated
stealth-browser scrape pool. Runs as a single Python process on Linux and macOS,
under systemd or launchd.

### Added

**Search**
- `GET /search` with engine rotation (Google, Bing, DuckDuckGo, Mojeek), a FIFO
  queue and a randomized 15–30s throttle.
- Circuit breaker with two-stage quarantine (30 minutes, then 12 hours),
  persisted to SQLite so a restart does not forget it.
- SQLite response cache keyed by a hash of the normalized query, 24h TTL.
- One persistent Chromium, one browser context per engine so cookies and DOM
  state never cross sites.

**Scrape**
- `GET /scrape` with a fast-path HTTP leg that escalates to a stealth-browser
  worker when a page is blocked, challenge-walled, suspiciously thin, or when
  `render=1` is requested.
- Browser worker: lazy Chromium start, a fresh browser context per job, and a
  deterministic idle shutdown that leaves no orphaned browser processes.
- Sitemap detection and `<loc>` extraction, block/challenge detection, and
  trafilatura-based text and link extraction (the document is parsed once).
- Per-host politeness (lock + minimum gap) with idle state eviction.
- Bounded TTL caches for both search and scrape results.

**Operations**
- `GET /health`, `/health/live` (cheap, for probes) and `/health/ready`, plus
  `GET /status`.
- Swagger UI at `/docs`, ReDoc at `/redoc`, OpenAPI schema at `/openapi.json`,
  with per-endpoint summaries, typed parameters, response schemas, worked
  examples and every error code each endpoint can return. `DOCS_ENABLED=false`
  removes them.
- `Makefile` targets for install, browser setup, lint, typecheck, test and run.
- Deployment examples for systemd (`deploy/muninn-api.service`) and launchd
  (`deploy/com.muninn.api.plist`), both running unprivileged.
- `ruff` lint/format and `mypy` type checking, both clean, enforced in CI on
  Python 3.10 and 3.12.

### Security

- **SSRF guard.** `/scrape` refuses loopback, link-local (including cloud
  metadata), private, multicast and reserved targets, and rejects non-HTTP
  schemes. Enforced in the API and again in the worker, before the cache lookup.
  Overridable with `SCRAPE_ALLOW_PRIVATE_TARGETS` or `SCRAPE_ALLOWED_HOSTS`. The
  DNS-rebinding exposure is documented rather than papered over.
- **robots.txt policy** for scrape targets, on by default, failing open when
  unreachable, with a bounded per-origin cache.
- **Rate limiting** on `/scrape` (429 + `Retry-After`) with bounded state.
- **Safe-by-default binding**: the service listens on `127.0.0.1`; the
  documented production command binds `0.0.0.0` and the security section says
  plainly what that exposes.
- `MIT` license. The service is explicitly single-user and unauthenticated; see
  `SECURITY.md` for the threat model and a hardening checklist.

### Fixed

- **Chromium leaked in externally supervised mode.** The deterministic teardown
  (process snapshot, group signal, detached reaper) was gated on
  `SCRAPE_WORKER_MODE=subprocess`, so a worker run as its own service — the
  normal production setup — logged "shut down" while Chromium processes
  survived and accumulated, one idle cycle leaking a few more. The teardown now
  runs in both modes; only the process exit is mode-specific.
- **The reaper could kill the worker.** Because the reaper child calls
  `setsid()`, it no longer shares the worker's process group, so a guard that
  compared against its own group never matched and the reaper's `killpg` pass
  signalled the worker itself. The worker's pid and group are now passed in
  explicitly and excluded.
- **`lxml_html_clean` was missing from the requirements.** The import chain
  `trafilatura → justext → lxml.html.clean` needs it at runtime, but no
  distribution declares it as a hard requirement (`lxml` ships it only as the
  optional extra `html-clean`), so a fresh install could fail at import. Now
  pinned, with a comment explaining why it must not be removed.
- **`Settings.port` was dead config.** A bare `uvicorn app.main:app` used
  uvicorn's own default rather than `PORT`. `python -m app.main` now serves on
  `Settings.host`/`Settings.port`, and a test keeps the port consistent across
  the settings, the Makefile, the README and the service units.

### Notes

- The requirements are fully pinned and the suite is hermetic: 233 tests, no
  network and no browser, in about 8 seconds. Verified locally on Python 3.10 and
  3.12; CI also covers 3.13.
- Platform support is Linux and macOS (the browser teardown uses POSIX process
  groups, `setsid` and `pgrep`/`ps`).
- There is no container image; see the README for service-based deployment.

[0.1.0]: https://github.com/reachdevel/Muninn/releases/tag/v0.1.0
