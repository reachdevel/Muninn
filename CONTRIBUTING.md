# Contributing to Muninn

Thanks for considering a contribution. This document covers how to get set up,
what we expect from a change, and how to get it merged.

## Development setup

Muninn targets **Linux and macOS** (it uses POSIX process groups, `setsid`, and
`pgrep`/`ps` for its browser teardown). Windows is not supported.

```bash
git clone <your fork>
cd muninn
make venv            # Python 3.10+ (3.12 recommended)
make install         # runtime + dev dependencies
make browsers        # download the pinned Chromium build
```

Or, by hand:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m playwright install chromium
```

Run the service while developing:

```bash
make run             # uvicorn with reload on 127.0.0.1:9999
```

## Before you open a pull request

```bash
make check           # ruff + mypy + pytest, exactly what CI runs
```

All three must pass. `make format` applies ruff's formatter; it is **not** a CI
gate, so you only need to run it on the files you touch.

The whole-pipeline load smoke test lives in `tests/test_batch_smoke.py` and runs
with the rest of the suite.

House rules the tooling cannot check for you:

- **Keep the test suite offline and fast.** No test may touch the network, spawn
  a real Chromium, or sleep for more than a few hundred milliseconds. Inject
  dependencies through the existing factories (`create_app(driver_factory=...)`,
  `FastPathFetcher(client_factory=...)`, `ScrapeService(validator=..., robots=...)`).
- **Update the docs with the code.** A new environment variable belongs in
  `README.md` and in `app/config.py`; a new endpoint belongs in the README's API
  section.
- **Document every endpoint in the OpenAPI schema.** Swagger UI at `/docs` is
  generated from the decorators, so a new endpoint needs a `summary`, a
  `description`, a `response_model` or a `responses` block, and an entry in
  `openapi_tags` if it introduces a new tag. `tests/test_api.py` asserts the
  schema keeps up.
- **Write comments for *why*.** The codebase explains reasoning, not mechanics.
  Match that.
- **No new runtime dependencies without discussion.** They change the install
  story for everyone.
  story for everyone.

## Commit and PR conventions

- One logical change per commit; write imperative subjects
  (`fix(scrape): ...`, `feat(search): ...`, `docs: ...`).
- Reference the issue the PR closes.
- Describe *why* in the PR body. The diff already shows *what*.

## Reporting bugs

Open an issue with the Muninn version, your OS and Python version, the exact
request or command, and what you expected. For anything security-related,
follow `SECURITY.md` instead - do not open a public issue.

## Adding a search engine

`drivers/parsers/` holds one module per engine:

1. Subclass `BaseParser` and set `ENGINE_NAME`, `BASE_URL` and
   `BLOCK_SIGNATURES`.
2. Implement `search_url()` and `parse()`; use the `_text()` / `_abs_url()`
   helpers.
3. Register it in `drivers/parsers/__init__.py` and add the name to
   `SUPPORTED_ENGINES` in `app/config.py`.
4. Add a fixture HTML page under `tests/` and a test that parses it.

The engine joins the existing circuit breaker and round-robin rotation with no
further changes.

## When an existing engine stops returning results

Search engines change their markup without notice, and a parser that matched
last quarter can silently start returning zero results. This is the one failure
mode that no offline test can catch, because the tests run against saved HTML.

Check it in this order:

1. Run the live probe — it exercises every engine with one real query each:

   ```bash
   LIVE=1 python scripts/live_probe.py "python web scraping"
   ```

   It prints a per-engine result count and the first hit, so a broken parser is
   obvious. This sends four real requests to search engines from your IP, which
   is why it is opt-in and why the default settings throttle it.

2. If an engine returns `0 results`, its `parse()` selectors no longer match.
   Fetch the search URL, save the response into `tests/html_fixtures.py`, and
   fix the CSS selectors in that engine's parser. **Refresh the fixture while
   you are there** — the offline tests are only as good as the HTML they were
   written against, and a stale fixture hides exactly the bug you just found.

3. If an engine returns a block page, that is the circuit breaker working: the
   engine is quarantined (30 minutes, then 12 hours) and `/status` shows it.
   Check `BLOCK_SIGNATURES` for that engine.

All four parsers were last verified against live markup on **2026-09-26**.

## Code of conduct

Be decent to each other. See `CODE_OF_CONDUCT.md`.
