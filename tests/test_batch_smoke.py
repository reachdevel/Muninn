"""Whole-pipeline load smoke test: 100+ searches through the real app.

This used to be ``scripts/batch_smoke.py``, but it imported its fakes from
``tests.conftest`` - so a script depended on the test package, and a packaged
deployment (which excludes ``tests/``) could not run it at all. It is a test, so it
lives here now, and CI runs it on every push.

Covers, with a simulated driver and no network:
  * 25 unique queries complete and are NOT served from cache;
  * the second wave of identical queries is served from cache with zero
    additional outbound calls;
  * round-robin rotation exercises every engine;
  * a blocked engine is quarantined and skipped, and when every engine is
    blocked the API answers 503.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import SUPPORTED_ENGINES
from app.main import create_app
from tests.conftest import FakeDriver, make_test_settings

UNIQUE_QUERIES = 25
REPEAT_EACH = 4
TOTAL = UNIQUE_QUERIES * REPEAT_EACH


def test_batch_smoke_throughput_and_cache() -> None:
    fake = FakeDriver()
    # This test's purpose is to exercise *every* engine, so it asks for plain
    # rotation: the default `grouped` strategy deliberately never touches the
    # fallback group while the primary one is healthy.
    app = create_app(
        settings=make_test_settings(search_strategy="round_robin"),
        driver_factory=lambda s: fake,
    )
    failures: list[str] = []

    with TestClient(app) as client:
        # --- wave 1: 25 unique queries (every engine must be exercised) -----
        wave1_engines: dict[str, int] = {}
        for i in range(UNIQUE_QUERIES):
            r = client.get("/search", params={"q": f"batch query {i}"})
            if r.status_code != 200:
                failures.append(f"wave1 q{i} -> {r.status_code}")
                continue
            body = r.json()
            wave1_engines[body["engine_used"]] = wave1_engines.get(body["engine_used"], 0) + 1
            if body["cached"]:
                failures.append(f"wave1 q{i} unexpectedly served from cache")
            if body["results_count"] < 1 or not body["results"]:
                failures.append(f"wave1 q{i} returned no results")

        # --- wave 2: identical queries -> cache hits, zero outbound calls ----
        calls_after_wave1 = fake.url_count
        cache_hits = 0
        for i in range(TOTAL - UNIQUE_QUERIES):
            r = client.get("/search", params={"q": f"  batch query {i % UNIQUE_QUERIES} "})
            if r.status_code != 200:
                failures.append(f"wave2 #{i} -> {r.status_code}")
                continue
            if r.json()["cached"]:
                cache_hits += 1

        # --- cache must have absorbed the second wave entirely --------------
        if cache_hits != TOTAL - UNIQUE_QUERIES:
            failures.append(
                f"expected {TOTAL - UNIQUE_QUERIES} cache hits, got {cache_hits}"
            )
        if fake.url_count != calls_after_wave1:
            failures.append("wave 2 issued outbound calls despite cache hits")

        # --- round-robin covered every engine -------------------------------
        uncovered = [e for e in SUPPORTED_ENGINES if wave1_engines.get(e, 0) == 0]
        if uncovered:
            failures.append(f"engines never exercised: {uncovered}")

        # --- circuit breaker ------------------------------------------------
        fake.block_engines = {"google"}
        if client.get("/search", params={"q": "block probe"}).json()["engine_used"] == "google":
            failures.append("google not excluded after block")
        fake.block_engines = set()
        fake.all_blocked = True
        if client.get("/search", params={"q": "all blocked"}).status_code != 503:
            failures.append("expected 503 when all engines are quarantined")
        fake.all_blocked = False

        status = client.get("/status").json()
        if all(e["status"] != "quarantined" for e in status["engines"].values()):
            failures.append("expected quarantine states in /status")
        final_status = client.get("/status").json()
        if final_status["queue_depth"] != 0:
            failures.append("queue did not drain")

    assert not failures, "batch smoke failures:\n  " + "\n  ".join(failures)
