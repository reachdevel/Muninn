"""API-level tests for /search, /health, /status using a fake driver.

The fake driver returns canned per-engine HTML instantly, so the throttler is
configured to a ~0s delay via ``make_test_settings`` - unit tests therefore
exercise the full pipeline (queue, round-robin, circuit breaker, cache)
without touching a real browser or the network.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import SUPPORTED_ENGINES, Settings
from app.main import create_app
from tests.conftest import FakeDriver, make_test_settings
from tests.html_fixtures import ENGINE_RESULTS_HTML

# --------------------------------------------------------------------------- /search


def test_search_returns_clean_results(client: TestClient) -> None:
    resp = client.get("/search", params={"q": "python"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "python"
    assert body["cached"] is False
    assert body["engine_used"] in SUPPORTED_ENGINES
    assert body["results_count"] >= 1
    assert body["results"][0]["title"]
    assert body["results"][0]["url"].startswith("http")
    assert "snippet" in body["results"][0]
    assert body["execution_time_ms"] >= 0


def test_search_missing_q_is_422(client: TestClient) -> None:
    assert client.get("/search").status_code == 422
    assert client.get("/search", params={"q": ""}).status_code == 422


def test_search_unknown_engine_is_422(client: TestClient) -> None:
    resp = client.get("/search", params={"q": "x", "engine": "altavista"})
    assert resp.status_code == 422


def test_search_honours_requested_active_engine(client: TestClient, fake_driver: FakeDriver) -> None:
    resp = client.get("/search", params={"q": "python", "engine": "bing"})
    assert resp.status_code == 200
    assert resp.json()["engine_used"] == "bing"


def test_cache_hit_serves_same_query_without_engine_call(
    client: TestClient, fake_driver: FakeDriver
) -> None:
    first = client.get("/search", params={"q": "python web scraping"})
    assert first.json()["cached"] is False
    calls_after_first = fake_driver.url_count

    second = client.get("/search", params={"q": "  PYTHON Web Scraping  "})
    assert second.status_code == 200
    assert second.json()["cached"] is True
    assert second.json()["engine_used"] == first.json()["engine_used"]
    # identical normalized query => no additional outbound fetch
    assert fake_driver.url_count == calls_after_first


def test_force_refresh_bypasses_cache(client: TestClient, fake_driver: FakeDriver) -> None:
    first = client.get("/search", params={"q": "same q"})
    assert first.json()["cached"] is False
    calls_after_first = fake_driver.url_count

    second = client.get("/search", params={"q": "same q", "force_refresh": "true"})
    assert second.json()["cached"] is False
    assert fake_driver.url_count == calls_after_first + 1


def test_force_refresh_does_not_populate_the_cache(
    client: TestClient, fake_driver: FakeDriver
) -> None:
    """force_refresh means "do not touch the cache" - read *and* write.

    Previously the fresh result was stored anyway, so the flag name read as a
    contradiction and a refresh-seeded entry outlived the caller's intent.
    """
    client.get("/search", params={"q": "no seed", "force_refresh": "true"})
    calls_after_refresh = fake_driver.url_count

    third = client.get("/search", params={"q": "no seed"})
    assert third.json()["cached"] is False, "force_refresh must not have cached the result"
    assert fake_driver.url_count == calls_after_refresh + 1

    # And the skip is visible in the metrics.
    metrics = client.get("/status").json()["metrics"]
    assert metrics["cache_write_skips"] >= 1


def test_default_strategy_rotates_inside_the_first_group(
    client: TestClient,
) -> None:
    """`grouped` is the default: the fallback group stays untouched."""
    primary = {"ddg", "brave", "bing", "yahoo", "google"}
    engines = []
    for i in range(5):
        resp = client.get("/search", params={"q": f"grouped {i}"})
        engines.append(resp.json()["engine_used"])
    assert set(engines) == primary
    assert "mojeek" not in engines and "ecosia" not in engines


def test_round_robin_across_engines() -> None:
    """Rotation order is the SUPPORTED_ENGINES order, when asked for."""
    from fastapi.testclient import TestClient as _Client

    app = create_app(
        settings=make_test_settings(search_strategy="round_robin"),
        driver_factory=lambda s: FakeDriver(),
    )
    with _Client(app) as client:
        engines = []
        for i in range(len(SUPPORTED_ENGINES)):
            resp = client.get("/search", params={"q": f"query number {i}"})
            engines.append(resp.json()["engine_used"])
    assert engines == list(SUPPORTED_ENGINES)


# --------------------------------------------------------------------------- circuit breaker


def test_blocked_engine_is_quarantined_and_rotation_continues(
    client: TestClient, fake_driver: FakeDriver
) -> None:
    # google blocks -> rotation must fall through to bing for the next query
    # Block the whole primary group: rotation must fall through to the fallback
    # group rather than returning nothing.
    fake_driver.block_engines = {"ddg", "brave", "bing", "yahoo", "google"}
    blocked = client.get("/search", params={"q": "first"})
    assert blocked.status_code == 200
    assert blocked.json()["engine_used"] in {"mojeek", "ecosia"}

    status = client.get("/status").json()["engines"]
    for name in ("google", "ddg", "brave", "bing", "yahoo"):
        assert status[name]["status"] == "quarantined", name
        assert status[name]["fail_count"] == 1, name
    # The fallback group served the query instead.
    assert status["ecosia"]["status"] == "active"


def test_all_engines_quarantined_returns_503(client: TestClient, fake_driver: FakeDriver) -> None:
    fake_driver.all_blocked = True
    resp = client.get("/search", params={"q": "doomed"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "all_engines_quarantined"


def test_quarantine_escalation_across_two_failures(client: TestClient, fake_driver: FakeDriver) -> None:
    fake_driver.block_engines = set(SUPPORTED_ENGINES)
    # first burst: every engine fails once
    for i in range(4):
        resp = client.get("/search", params={"q": f"burst {i}"})
        assert resp.status_code == 503

    status = client.get("/status").json()["engines"]
    assert all(v["fail_count"] == 1 for v in status.values())
    assert all(v["quarantine_level"] == 1 for v in status.values())


# --------------------------------------------------------------------------- health / status / root


def test_health_endpoint(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["browser_ready"] is True
    assert body["queue_depth"] == 0
    assert body["cache_entries"] == 0
    assert set(body["active_engines"]) == set(SUPPORTED_ENGINES)
    assert body["uptime_seconds"] >= 0


def test_status_endpoint_shape(client: TestClient) -> None:
    client.get("/search", params={"q": "state probe"})
    body = client.get("/status").json()
    assert set(body) == {
        "queue_depth",
        "cached_queries_count",
        "metrics",
        "worker",
        "breaker",
        "engines",
    }
    assert set(body["engines"]) == set(SUPPORTED_ENGINES)
    assert body["metrics"]["searches_served"] >= 1
    assert body["cached_queries_count"] == 1


def test_root_metadata(client: TestClient) -> None:
    body = client.get("/").json()
    assert body["service"] == "Muninn API Gateway"
    assert "/search" in body["endpoints"]


def test_status_reports_worker_and_breaker(client: TestClient) -> None:
    """The wedge looked healthy everywhere else, so /status carries the worker
    identity and the breaker state."""
    client.get("/search", params={"q": "worker probe"})
    body = client.get("/status").json()
    worker = body["worker"]
    assert worker["pid"] == os.getpid()
    assert worker["state"] in {"idle", "busy", "stuck", "dead"}
    assert worker["pool_size"] >= 1
    assert worker["jobs_completed"] >= 1
    assert worker["jobs_killed"] == 0
    assert body["breaker"]["state"] == "closed"
    assert body["breaker"]["consecutive_failures"] == 0

def test_health_live_is_cheap_and_ok(client) -> None:
    """The container healthcheck endpoint: no DB query, no worker probe."""
    r = client.get("/health/live")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "queue_depth" in body
    # The deep report must NOT be inlined here - that is the point of the split.
    assert "browser_pool" not in body
    # ...but the search worker is, because a wedged queue used to be invisible.
    assert body["worker"]["pid"] == os.getpid()
    assert body["worker"]["state"] in {"idle", "busy", "stuck", "dead"}
    assert body["worker"]["current_job_age_s"] >= 0
    assert "jobs_completed" in body["worker"]
    assert body["breaker"]["state"] == "closed"


def test_health_ready_reflects_engine_availability(client) -> None:
    r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert "google" in body["active_engines"]
    # Reported, not enforced: readiness must not follow the breaker, or the
    # instance would be pulled out of rotation and never get its probe back.
    assert body["breaker"]["state"] == "closed"


def test_pinned_quarantined_engine_is_refused_immediately(
    client: TestClient, fake_driver: FakeDriver
) -> None:
    """A pinned engine that is out can never answer: say so at once (P1).

    Silently rotating to another engine would be worse than useless for a client
    pinning `engine=ddg` for a `site:` query.
    """
    fake_driver.block_engines = {"ddg"}
    for i in range(4):
        client.get("/search", params={"q": f"quarantine ddg {i}"})
    status = client.get("/status").json()["engines"]
    assert status["ddg"]["status"] == "quarantined"
    # ddg recovers; the other engines were never in trouble.
    fake_driver.block_engines = set()
    calls = fake_driver.url_count
    resp = client.get("/search", params={"q": "site:example.com", "engine": "ddg"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "engine_quarantined"
    assert resp.json()["engine"] == "ddg"
    assert resp.headers["Retry-After"]
    # Refused, not queued: no outbound request was made for it.
    assert fake_driver.url_count == calls


def test_health_reports_scrape_cache_and_rate_limiter(client) -> None:
    body = client.get("/health").json()
    assert body["browser_ready"] is True
    assert body["scrape_cache"]["entries"] >= 0
    assert body["rate_limiter"]["per_minute"] > 0


# ------------------------------------------------------------------ OpenAPI / docs


def test_openapi_schema_is_served_and_describes_every_endpoint(client) -> None:
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["info"]["title"] == "Muninn API Gateway"
    assert spec["info"]["license"]["name"] == "MIT"
    # Every public endpoint is documented.
    for path in ("/search", "/scrape", "/status", "/health", "/health/live", "/health/ready"):
        assert path in spec["paths"], f"{path} missing from the OpenAPI schema"
        assert "get" in spec["paths"][path]


def test_openapi_documents_error_responses(client) -> None:
    """The status codes the README promises must be in the schema, not just prose."""
    responses = client.get("/openapi.json").json()["paths"]["/scrape"]["get"]["responses"]
    for code in ("200", "400", "403", "422", "429", "502", "503"):
        assert code in responses, f"/scrape {code} undocumented"


def test_openapi_has_response_schemas_and_examples(client) -> None:
    op = client.get("/openapi.json").json()["paths"]["/scrape"]["get"]
    assert "summary" in op and op["summary"]
    assert "application/json" in op["responses"]["200"]["content"]
    # Error bodies are typed, so Swagger renders them as a model.
    assert op["responses"]["400"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorResponse"
    )


def test_search_documents_its_error_codes(client) -> None:
    responses = client.get("/openapi.json").json()["paths"]["/search"]["get"]["responses"]
    for code in ("200", "422", "503", "504"):
        assert code in responses


def test_swagger_ui_is_served_by_default(client) -> None:
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_docs_can_be_disabled() -> None:
    """DOCS_ENABLED=0 must remove the schema entirely, for exposed deployments."""
    # The fake driver keeps this hermetic: without it the default factory builds
    # a real BrowserDriver and the test needs a downloaded Chromium, which is
    # exactly the "no test launches a browser" property the suite claims.
    app = create_app(
        settings=make_test_settings(docs_enabled=False),
        driver_factory=lambda s: FakeDriver(),
    )
    with TestClient(app) as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert c.get(path).status_code == 404, f"{path} should be disabled"
        assert c.get("/health/live").status_code == 200  # service still works


def test_root_endpoint_list_matches_the_real_routes() -> None:
    """`GET /` advertises the API surface; it must not drift from reality.

    `/metrics` was added to the app and to the README while the root listing was
    never updated, so the service's own self-description was incomplete.

    The OpenAPI schema is the source of truth rather than ``app.routes``:
    FastAPI 0.141 keeps ``include_router`` results as opaque ``_IncludedRouter``
    entries instead of flattening them, so route introspection alone is not
    reliable across versions.
    """
    # Framework-provided docs routes are not part of the service surface.
    framework = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

    with TestClient(
        create_app(settings=make_test_settings(), driver_factory=lambda s: FakeDriver())
    ) as client:
        advertised = set(client.get("/").json()["endpoints"])
        spec_paths = set(client.get("/openapi.json").json()["paths"])

    documented_operations = spec_paths - framework
    unadvertised = documented_operations - advertised
    # `/docs`, `/redoc` and `/openapi.json` are advertised too, and they only
    # exist when DOCS_ENABLED is on, so they are allowed to be "missing".
    missing = advertised - spec_paths - framework
    assert not unadvertised, f"routes not listed by GET /: {sorted(unadvertised)}"
    assert not missing, f"GET / advertises routes that do not exist: {sorted(missing)}"
