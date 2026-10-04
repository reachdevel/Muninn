"""Environment-driven global configuration for Muninn.

Every tunable (throttle delays, quarantine durations, cache TTL, browser
settings) lives here and can be overridden with an environment variable so the
behaviour is identical on a home PC, in CI, and under a service manager.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Engine pool registered for Round-Robin rotation.
#
# Implemented and tested, deliberately not in the rotation: yandex and qwant both
# answer every live probe from this host with a challenge wall rather than results
# (SmartCaptcha and DataDome respectively), and a warm context does not change
# either answer. Their parsers stay in drivers/parsers (and in PARSER_REGISTRY) so
# they are one line away if another exit IP or a future approach gets through;
# adding the name to this tuple is all it takes.
#
# Ecosia is here because its wall turned out to be a *cookie* wall, not an IP
# block - the driver warms the context from the engine's WARMUP_URL and is served
# normally. See drivers/parsers/ecosia.py for the evidence.
SUPPORTED_ENGINES: tuple[str, ...] = (
    "google", "bing", "ddg", "mojeek", "brave", "ecosia", "yahoo",
)


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Global, immutable settings for the gateway service."""

    # --- Throttling ---------------------------------------------------------
    # Randomized delay enforced between consecutive outbound search requests.
    throttle_min_delay: float = field(default_factory=lambda: _env_float("THROTTLE_MIN_DELAY", 15.0))
    throttle_max_delay: float = field(default_factory=lambda: _env_float("THROTTLE_MAX_DELAY", 30.0))

    # --- Circuit breaker / quarantine --------------------------------------
    # Cooldowns are exponential per consecutive failure and *capped*:
    # QUARANTINE_FIRST_SECONDS is the base (scaled down for weaker failure
    # classes, see engine_manager.FAILURE_CLASS_WEIGHTS) and
    # QUARANTINE_ESCALATED_SECONDS is the ceiling. A 12h ceiling could take the
    # pool from four engines to one; 30m re-probes often and keeps capacity.
    quarantine_first_seconds: int = field(default_factory=lambda: _env_int("QUARANTINE_FIRST_SECONDS", 300))
    quarantine_escalated_seconds: int = field(default_factory=lambda: _env_int("QUARANTINE_ESCALATED_SECONDS", 1_800))

    # --- Caching ------------------------------------------------------------
    # TTL for identical-query responses (24-48h default, spec section 2.2).
    cache_ttl_seconds: int = field(default_factory=lambda: _env_int("CACHE_TTL_SECONDS", 86_400))
    cache_db_path: str = field(default_factory=lambda: os.environ.get("CACHE_DB_PATH", "data/cache.db"))

    # --- API ----------------------------------------------------------------
    default_max_results: int = field(default_factory=lambda: _env_int("DEFAULT_MAX_RESULTS", 10))
    max_max_results: int = field(default_factory=lambda: _env_int("MAX_MAX_RESULTS", 50))
    request_timeout_seconds: int = field(default_factory=lambda: _env_int("REQUEST_TIMEOUT_SECONDS", 120))
    # The search queue is bounded: throughput is ~2-4 requests/minute because of
    # the throttle, so a deeper queue only accumulates work that will time out.
    # Requests arriving when it is full get 503 with Retry-After.
    max_search_queue: int = field(default_factory=lambda: _env_int("MAX_SEARCH_QUEUE", 100))
    # Per-client budget for /search (abuse protection, not authentication).
    search_rate_limit_per_minute: int = field(
        default_factory=lambda: _env_int("SEARCH_RATE_LIMIT_PER_MINUTE", 30)
    )

    # --- Engine selection strategy -------------------------------------------
    # How the next engine is chosen for a job.
    #   grouped    - round-robin inside the first group that has any usable
    #                engine, falling back to the next group. Default, because it
    #                keeps flaky engines off the hot path: a blocked engine in
    #                group 1 is quarantined and skipped, while group 2 is only
    #                touched when everything above it is out.
    #   round_robin- plain rotation across every active engine.
    #   priority   - try engines in SUPPORTED_ENGINES order and fall through on
    #                failure, like a failover list.
    search_strategy: str = field(
        default_factory=lambda: os.environ.get("SEARCH_STRATEGY", "grouped").lower()
    )
    # Priority groups for the `grouped` strategy, pipe-separated and tried left to
    # right. Any engine missing from this list is appended to a final group rather
    # than becoming unreachable, so adding an engine cannot silently lose it.
    search_engine_groups: str = field(
        default_factory=lambda: os.environ.get(
            "SEARCH_ENGINE_GROUPS", "ddg,brave,bing,yahoo,google|mojeek,ecosia"
        )
    )

    # --- Search region -------------------------------------------------------
    # Search engines geolocate by exit IP, so a server in Germany is shown German
    # results first no matter what the browser's Accept-Language says. This is the
    # market the SERPs are requested for, as a two-letter country code. It is
    # applied per engine in drivers/parsers, and only where an engine actually
    # honours a region parameter - see the comments there for the ones that
    # ignore it, or break if you set it.
    search_region: str = field(default_factory=lambda: os.environ.get("SEARCH_REGION", "us"))

    # --- Search job execution (P0: the worker must always be released) ------
    # Hard deadline for ONE search job: throttle wait, every engine attempt and
    # any retry inside it. On expiry the job is aborted, the engine that was in
    # flight is charged one failure, and the worker moves on. This is the
    # guarantee that a hung upstream call cannot pin the worker forever - the
    # P0 fix for the wedge where /health stayed fast while /search never
    # returned. Keep it well under REQUEST_TIMEOUT_SECONDS so a job finishes
    # (or dies) before the caller gives up on it.
    search_job_deadline_seconds: float = field(
        default_factory=lambda: _env_float("SEARCH_JOB_DEADLINE_SECONDS", 45.0)
    )
    # A job must BEGIN executing within this long of being enqueued. The throttle
    # wait counts against it: a job waiting its turn is not making progress, and
    # on a throttle-bound service a queue that cannot start a job inside this
    # window never will. Expiry is a fast 503 (come back later), not a 504 - the
    # job never ran, so nothing failed.
    search_queue_wait_deadline_seconds: float = field(
        default_factory=lambda: _env_float("SEARCH_QUEUE_WAIT_DEADLINE_SECONDS", 45.0)
    )
    # Workers draining the queue. More than one isolates a hanging engine: it
    # can only occupy one worker, so the healthy engines keep serving.
    search_worker_count: int = field(default_factory=lambda: _env_int("SEARCH_WORKER_COUNT", 2))
    # Max jobs allowed against a single engine at a time.
    search_max_concurrent_per_engine: int = field(
        default_factory=lambda: _env_int("SEARCH_MAX_CONCURRENT_PER_ENGINE", 1)
    )
    # Grace over the job deadline before a still-busy worker is called stuck.
    search_worker_stall_grace_seconds: float = field(
        default_factory=lambda: _env_float("SEARCH_WORKER_STALL_GRACE_SECONDS", 30.0)
    )

    # --- Search load breaker (P0: refuse fast, recover on a probe) ---------
    # Consecutive failures (deadline kills, a worker that will not drain, a
    # dead worker) before the breaker starts refusing new search work.
    search_breaker_failure_threshold: int = field(
        default_factory=lambda: _env_int("SEARCH_BREAKER_FAILURE_THRESHOLD", 3)
    )
    # A non-empty queue with no job completed for this long is "not draining".
    # Must exceed the job deadline plus one throttle window, or a legitimately
    # slow queue would trip the breaker.
    search_breaker_stall_seconds: float = field(
        default_factory=lambda: _env_float("SEARCH_BREAKER_STALL_SECONDS", 120.0)
    )
    # How often the supervisor samples the queue and the workers.
    search_monitor_interval_seconds: float = field(
        default_factory=lambda: _env_float("SEARCH_MONITOR_INTERVAL_SECONDS", 5.0)
    )
    # How long the breaker refuses everything before admitting one probe, and
    # the ceiling it doubles towards on each failed probe round.
    search_breaker_open_seconds: float = field(
        default_factory=lambda: _env_float("SEARCH_BREAKER_OPEN_SECONDS", 30.0)
    )
    search_breaker_max_open_seconds: float = field(
        default_factory=lambda: _env_float("SEARCH_BREAKER_MAX_OPEN_SECONDS", 300.0)
    )

    # --- Browser driver -----------------------------------------------------
    headless: bool = field(default_factory=lambda: _env_bool("HEADLESS", True))
    page_load_timeout_ms: int = field(default_factory=lambda: _env_int("PAGE_LOAD_TIMEOUT_MS", 45_000))
    navigation_timeout_ms: int = field(default_factory=lambda: _env_int("NAVIGATION_TIMEOUT_MS", 60_000))
    browser_args: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            os.environ.get(
                "BROWSER_ARGS",
                "--disable-blink-features=AutomationControlled",
            ).split(",")
        )
    )
    # Chromium's sandbox is a real security boundary; it is off by default only
    # because it is unreliable inside minimal containers. Running Muninn as root
    # AND with the sandbox disabled means a browser escape is a host compromise,
    # so keep BROWSER_NO_SANDBOX=false where the sandbox works and grant the
    # capabilities Chromium needs (see deploy/muninn-api.service).
    browser_no_sandbox: bool = field(
        default_factory=lambda: _env_bool("BROWSER_NO_SANDBOX", True)
    )
    user_agent: str = field(
        default_factory=lambda: os.environ.get(
            "USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        )
    )
    locale: str = field(default_factory=lambda: os.environ.get("LOCALE", "en-US"))

    # --- Scrape module -----------------------------------------------------
    # Content extraction / response shaping.
    # default_max_text is what a caller gets when they do not ask for a size;
    # max_text_cap is the hard ceiling they may ask for (the endpoint's
    # advertised upper bound). Keeping them separate means a client can request
    # MORE than the default without the server silently clamping them to it.
    default_max_text: int = field(default_factory=lambda: _env_int("DEFAULT_MAX_TEXT", 32_000))
    max_text_cap: int = field(default_factory=lambda: _env_int("MAX_TEXT_CAP", 200_000))
    max_links_cap: int = field(default_factory=lambda: _env_int("MAX_LINKS_CAP", 60))
    # Pages whose extracted text is smaller than this (despite a large raw
    # HTML body) are treated as suspicious / blocked.
    text_min_char_threshold: int = field(default_factory=lambda: _env_int("TEXT_MIN_CHAR_THRESHOLD", 200))
    # A page whose raw HTML exceeds this is "large" for the thin-text rule.
    text_anomaly_html_bytes: int = field(default_factory=lambda: _env_int("TEXT_ANOMALY_HTML_BYTES", 20_480))

    # Scrape result cache (URL+render keyed, TTL, bounded LRU).
    scrape_cache_ttl: int = field(default_factory=lambda: _env_int("SCRAPE_CACHE_TTL", 3_600))
    scrape_cache_max_entries: int = field(
        default_factory=lambda: _env_int("SCRAPE_CACHE_MAX_ENTRIES", 2_000)
    )

    # Fast-path (plain HTTP) fetch.
    scrape_fast_path_timeout: float = field(default_factory=lambda: _env_float("SCRAPE_FAST_PATH_TIMEOUT", 20.0))
    scrape_max_body_bytes: int = field(default_factory=lambda: _env_int("SCRAPE_MAX_BODY_BYTES", 10_000_000))
    # Redirects are followed manually so every hop is re-validated by the SSRF
    # guard. Keep this small: each hop is a real request and a real DNS lookup.
    scrape_max_redirects: int = field(default_factory=lambda: _env_int("SCRAPE_MAX_REDIRECTS", 5))

    # Per-host politeness: minimum gap between consecutive requests to one
    # hostname (applies across the fast-path AND browser-render legs).
    per_host_delay_seconds: float = field(default_factory=lambda: _env_float("PER_HOST_DELAY_SECONDS", 2.0))
    # Idle time after which a host's politeness bookkeeping is discarded.
    politeness_idle_evict_seconds: float = field(
        default_factory=lambda: _env_float("POLITENESS_IDLE_EVICT_SECONDS", 300.0)
    )

    # --- Outbound safety guards (see ops/netguard.py, ops/robots.py) --------
    # SSRF guard: private/loopback/link-local targets are refused by default.
    scrape_allow_private_targets: bool = field(
        default_factory=lambda: _env_bool("SCRAPE_ALLOW_PRIVATE_TARGETS", False)
    )
    # Comma-separated allowlist that overrides the network checks entirely
    # (e.g. "mycorp.lan,10.0.0.5"). Empty = apply the SSRF rules.
    scrape_allowed_hosts: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            h.strip() for h in os.environ.get("SCRAPE_ALLOWED_HOSTS", "").split(",") if h.strip()
        )
    )
    # robots.txt policy for /scrape targets (fail-open when unreachable).
    scrape_respect_robots: bool = field(
        default_factory=lambda: _env_bool("SCRAPE_RESPECT_ROBOTS", True)
    )
    # Coarse per-client rate limit on /scrape (abuse protection, not auth).
    scrape_rate_limit_per_minute: int = field(
        default_factory=lambda: _env_int("SCRAPE_RATE_LIMIT_PER_MINUTE", 60)
    )

    # Stealth browser pool / worker process.
    browser_idle_timeout: int = field(default_factory=lambda: _env_int("BROWSER_IDLE_TIMEOUT", 300))
    browser_max_contexts: int = field(default_factory=lambda: _env_int("BROWSER_MAX_CONTEXTS", 1))
    scrape_render_timeout: float = field(default_factory=lambda: _env_float("SCRAPE_RENDER_TIMEOUT", 45.0))
    # "subprocess" (manager spawns/supervises python -m browser_pool.worker)
    # or "external" (worker runs as its own supervised service).
    scrape_worker_mode: str = field(default_factory=lambda: os.environ.get("SCRAPE_WORKER_MODE", "subprocess"))
    scrape_worker_host: str = field(default_factory=lambda: os.environ.get("SCRAPE_WORKER_HOST", "127.0.0.1"))
    scrape_worker_port: int = field(default_factory=lambda: _env_int("SCRAPE_WORKER_PORT", 8_765))
    scrape_worker_url: str = field(default_factory=lambda: os.environ.get("SCRAPE_WORKER_URL", ""))
    scrape_worker_startup_timeout: float = field(default_factory=lambda: _env_float("SCRAPE_WORKER_STARTUP_TIMEOUT", 30.0))
    scrape_worker_log_file: str = field(default_factory=lambda: os.environ.get("SCRAPE_WORKER_LOG_FILE", "data/scrape-worker.log"))

    # --- Service ------------------------------------------------------------
    # Bind address. Defaults to loopback so a bare `uvicorn app.main:app` is
    # not reachable from the network; a service unit overrides this to 0.0.0.0
    # because containers must bind all interfaces.
    host: str = field(default_factory=lambda: os.environ.get("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PORT", 9999))
    # Interactive API docs: Swagger UI at /docs, ReDoc at /redoc and the
    # OpenAPI schema at /openapi.json. On by default because they are the
    # fastest way to understand the API. The service has no authentication, so
    # set DOCS_ENABLED=0 anywhere other than a trusted machine.
    docs_enabled: bool = field(default_factory=lambda: _env_bool("DOCS_ENABLED", True))
    # Log format: "text" for a terminal, "json" for one object per line.
    log_format: str = field(default_factory=lambda: os.environ.get("LOG_FORMAT", "text"))
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))


    def browser_launch_args(self) -> list[str]:
        """Chromium flags for both browser launch sites."""
        args = list(self.browser_args)
        if self.browser_no_sandbox:
            args.append("--no-sandbox")
        return args


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


_settings: Settings | None = None