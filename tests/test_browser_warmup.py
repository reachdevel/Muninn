"""The per-engine context warm-up, without launching a browser.

Ecosia's wall is a cookie wall: a context arriving cold at a search URL gets
HTTP 403, while the same context that has loaded the homepage first is served
normally. The driver therefore visits the engine's ``WARMUP_URL`` once per
context. These tests use a fake Playwright browser, so they stay hermetic.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from drivers.browser_driver import BrowserDriver
from drivers.parsers import BraveParser, EcosiaParser, GoogleParser


class FakePage:
    def __init__(self, log: list[str], fail: bool = False) -> None:
        self._log = log
        self._fail = fail
        self.closed = False

    async def goto(self, url: str, **kwargs) -> None:
        self._log.append(url)
        if self._fail:
            raise RuntimeError("navigation failed")

    async def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        self._log.append(f"settle:{state}")

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, log: list[str], fail: bool = False) -> None:
        self._log = log
        self._fail = fail
        self.pages: list[FakePage] = []

    async def new_page(self) -> FakePage:
        page = FakePage(self._log, self._fail)
        self.pages.append(page)
        return page


class FakeBrowser:
    def __init__(self, log: list[str], fail: bool = False) -> None:
        self._log = log
        self._fail = fail
        self.contexts: list[FakeContext] = []

    async def new_context(self, **kwargs) -> FakeContext:
        context = FakeContext(self._log, self._fail)
        self.contexts.append(context)
        return context


def _driver(log: list[str], fail: bool = False) -> BrowserDriver:
    driver = BrowserDriver(Settings())
    driver._browser = FakeBrowser(log, fail)  # type: ignore[assignment]  # noqa: SLF001
    return driver


async def test_an_engine_with_a_warmup_url_is_warmed_once_per_context() -> None:
    log: list[str] = []
    driver = _driver(log)

    first = await driver._context_for("ecosia")  # noqa: SLF001
    second = await driver._context_for("ecosia")  # noqa: SLF001

    assert first is second, "the context must be reused, not recreated"
    # The warm-up happens once for the context, not once per request.
    assert log.count(EcosiaParser.WARMUP_URL) == 1
    # ...and it settles before closing, so the page's own scripts have run.
    assert any(entry.startswith("settle:") for entry in log)
    assert driver._browser.contexts[0].pages[0].closed  # noqa: SLF001


async def test_engines_without_a_warmup_url_pay_nothing() -> None:
    for engine in ("google", "bing", "ddg", "mojeek", "brave"):
        log: list[str] = []
        driver = _driver(log)
        await driver._context_for(engine)  # noqa: SLF001
        assert log == [], f"{engine} should not navigate anywhere to warm up"


async def test_a_failed_warmup_never_breaks_the_search_that_follows() -> None:
    """Best effort by design: a warm-up that fails must be invisible."""
    log: list[str] = []
    driver = _driver(log, fail=True)

    context = await driver._context_for("ecosia")  # noqa: SLF001
    assert context is not None
    assert log == [EcosiaParser.WARMUP_URL]
    assert driver._browser.contexts[0].pages[0].closed  # noqa: SLF001


async def test_the_warmup_url_is_declared_by_the_parser_not_the_driver() -> None:
    """The driver must not need engine-specific knowledge."""
    assert EcosiaParser.WARMUP_URL
    assert BraveParser.WARMUP_URL == ""
    assert GoogleParser.WARMUP_URL == ""


@pytest.mark.parametrize("engine", ["google", "ecosia"])
async def test_context_creation_is_serialised(engine: str) -> None:
    """Two concurrent requests for a cold engine must not race the context."""
    log: list[str] = []
    driver = _driver(log)
    contexts = await asyncio.gather(*(driver._context_for(engine) for _ in range(4)))  # noqa: SLF001
    assert all(c is contexts[0] for c in contexts)
    assert len(driver._browser.contexts) == 1  # noqa: SLF001