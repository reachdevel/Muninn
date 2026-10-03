"""Shared pytest fixtures: fake driver, tiny-delay settings, and TestClient app."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.html_fixtures import BLOCK_PAGES, ENGINE_RESULTS_HTML


class FakeDriver:
    """Stand-in for BrowserDriver that returns canned HTML per engine.

    Mirrors the real driver's public surface, including ``is_started`` - the
    health router reads that property, not the private ``_started`` flag.
    """

    def __init__(self, block_engines: set[str] | None = None, all_blocked: bool = False) -> None:
        self._started = False
        self.urls: list[str] = []
        self.block_engines = block_engines or set()
        self.all_blocked = all_blocked

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def url_count(self) -> int:
        return len(self.urls)

    @staticmethod
    def engine_from_url(url: str) -> str:
        if "google.com" in url:
            return "google"
        if "bing.com" in url:
            return "bing"
        if "duckduckgo" in url:
            return "ddg"
        if "mojeek.com" in url:
            return "mojeek"
        return "unknown"

    async def fetch_html(self, url: str, engine: str = "") -> tuple[str, int]:
        self.urls.append(url)
        key = engine or self.engine_from_url(url)
        if self.all_blocked or key in self.block_engines:
            return BLOCK_PAGES[key], 200
        return ENGINE_RESULTS_HTML[key], 200


def make_test_settings(**overrides) -> Settings:
    base = {
        "throttle_min_delay": 0.0,
        "throttle_max_delay": 0.0,
        "cache_db_path": ":memory:",
        "request_timeout_seconds": 10,
        # Outbound guards are exercised by their own unit tests; here they are
        # switched off so the suite performs no DNS lookups and no HTTP probes.
        "scrape_allow_private_targets": True,
        "scrape_respect_robots": False,
        # Rate limiters are off by default in tests: several tests legitimately
        # issue bursts, and a 429 would fail them for the wrong reason. The
        # limiter itself has dedicated tests.
        "scrape_rate_limit_per_minute": 100_000,
        "search_rate_limit_per_minute": 100_000,
        "max_search_queue": 1_000,
        # Short job deadline so a test that stalls an engine settles fast, and
        # coherent with request_timeout_seconds above (the service warns at
        # startup when the two together can outlast the caller).
        "search_job_deadline_seconds": 5.0,
        # Same for the queue wait: tests must not inherit a production-sized
        # budget and fail on a queue-wait refusal instead of what they test.
        "search_queue_wait_deadline_seconds": 5.0,
        # The supervisor samples fast here so a test does not have to sleep for
        # the production interval to notice a dead worker.
        "search_monitor_interval_seconds": 0.5,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def fake_driver() -> FakeDriver:
    return FakeDriver()


@pytest.fixture
def client(fake_driver: FakeDriver):
    app = create_app(
        settings=make_test_settings(),
        driver_factory=lambda s: fake_driver,
    )
    with TestClient(app) as test_client:
        yield test_client