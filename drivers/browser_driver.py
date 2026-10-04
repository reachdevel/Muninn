"""Persistent Playwright-stealth Chromium driver.

A single browser + single BrowserContext is started once (app lifespan) and
reused for every engine request. Each search opens a fresh ``Page`` in that
context, scrapes it, and closes it - so no browser process is ever launched or
torn down per request. Stealth evasions are injected automatically for every
page via ``playwright_stealth.Stealth.use_async``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright_stealth import Stealth

from app.config import Settings

logger = logging.getLogger(__name__)


class BrowserDriverError(Exception):
    """Raised when the underlying browser cannot fulfil a request."""


async def _settle(page: Page, quiet_ms: int = 1_500) -> None:
    """Wait for client-side rendering to finish before reading the DOM.

    Search engines paint results with JavaScript, so reading the DOM at
    ``domcontentloaded`` yields an empty page. Waiting for networkidle is the
    real signal, but a page with long-polling or analytics beacons never
    reaches it - so we race networkidle against a fixed ceiling and take
    whichever arrives first. That is faster than always sleeping, and it does
    not hang on beacon-heavy pages.
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=quiet_ms)
    except PWTimeoutError:
        # Ceiling reached: the page is busy or beacons never stop. Whatever has
        # rendered by now is still worth parsing.
        logger.debug("networkidle not reached; capturing DOM as-is")


class BrowserDriver:
    """Owns one persistent Chromium browser with a context per search engine."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stealth_cm: Stealth | None = None
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        # One BrowserContext per engine: contexts are cheap compared with
        # launching a browser, and they keep cookies/localStorage/DOM state from
        # leaking between sites.
        self._contexts: dict[str, BrowserContext] = {}
        self._started = False
        # Guards context creation only, never a navigation.
        self._lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------

    @property
    def is_started(self) -> bool:
        """Whether the persistent browser is up (used by the health router)."""
        return self._started

    async def start(self) -> None:
        """Launch the persistent browser and apply stealth hooks."""
        if self._started:
            return
        stealth = Stealth()
        self._stealth_cm = stealth.use_async(async_playwright())
        self._pw = await self._stealth_cm.__aenter__()

        self._browser = await self._pw.chromium.launch(
            headless=self._settings.headless,
            args=self._settings.browser_launch_args(),
        )
        self._started = True
        logger.info("Persistent Chromium started (stealth applied)")

    async def _context_for(self, engine: str) -> BrowserContext:
        """Return this engine's context, creating it on first use.

        One context per engine, reused for the life of the browser. Sharing a
        single context across engines would let cookies, localStorage and DOM
        state bleed from Google into Bing, which is both a correctness problem
        and a bot-detection signal. Contexts are cheap relative to launching a
        browser, so this keeps the "no process churn" property while isolating
        the sites.
        """
        assert self._browser is not None  # guaranteed by is_started / start()
        async with self._lock:
            context = self._contexts.get(engine)
            if context is None:
                context = await self._browser.new_context(
                    user_agent=self._settings.user_agent,
                    locale=self._settings.locale,
                    viewport={"width": 1280, "height": 900},
                )
                self._contexts[engine] = context
                logger.debug("created browser context for engine=%s", engine)
                await self._warm_up(engine, context)
            return context

    async def _warm_up(self, engine: str, context: BrowserContext) -> None:
        """Load the engine's warm-up page once per context, if it declares one.

        Best effort by design: a failed warm-up must not fail the search that
        follows, since for most engines it does not exist and for the rest it only
        improves the odds. Its cost is one extra page load, once per context.
        """
        warmup_url = ""
        try:
            from drivers.parsers import get_parser

            warmup_url = get_parser(engine).WARMUP_URL
        except KeyError:
            return
        except Exception:  # pragma: no cover - never let a warm-up break a search
            logger.debug("could not resolve a warm-up url for engine=%s", engine, exc_info=True)
            return
        if not warmup_url:
            return
        page: Page | None = None
        try:
            page = await context.new_page()
            await page.goto(
                warmup_url,
                wait_until="domcontentloaded",
                timeout=self._settings.navigation_timeout_ms,
            )
            # Settle exactly as a real page load does. Closing at domcontentloaded
            # is not enough: Ecosia's consent script has not run yet, so no
            # cookies are stored, and the search that follows gets the firewall -
            # which made the warm-up work only intermittently.
            await _settle(page)
            logger.debug("warmed up engine=%s via %s", engine, warmup_url)
        except Exception:
            logger.debug("warm-up failed for engine=%s via %s", engine, warmup_url, exc_info=True)
        finally:
            if page is not None:
                with suppress(Exception):
                    await page.close()

    async def stop(self) -> None:
        """Tear down every context, the browser, and the playwright session."""
        for engine, context in list(self._contexts.items()):
            try:
                await context.close()
            except Exception:  # pragma: no cover - best effort shutdown
                logger.debug("error closing context for %s", engine, exc_info=True)
        self._contexts.clear()
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # pragma: no cover
                logger.debug("error closing browser", exc_info=True)
            self._browser = None
        if self._stealth_cm is not None and self._pw is not None:
            try:
                await self._stealth_cm.__aexit__(None, None, None)
            except Exception:  # pragma: no cover
                logger.debug("error closing playwright", exc_info=True)
        self._stealth_cm = None
        self._pw = None
        self._browser = None
        self._started = False
        logger.info("Persistent Chromium stopped (all engine contexts closed)")

    # -- request helpers ----------------------------------------------------

    async def fetch_html(self, url: str, engine: str = "") -> tuple[str | None, int | None]:
        """Navigate to ``url`` in a fresh page and return ``(html, http_status)``.

        ``engine`` selects which per-engine context the page is opened in, so
        state never crosses sites. The page is always closed before returning;
        the browser and contexts are preserved for the next request. A hard
        per-page time budget prevents a misbehaving engine page from hanging the
        queue forever.
        """
        if not self._started or self._browser is None:
            raise BrowserDriverError("browser driver is not started")
        context = await self._context_for(engine or url)
        budget = max(30.0, self._settings.navigation_timeout_ms / 1000 + 15)

        async def _fetch() -> tuple[str | None, int | None]:
            page: Page = await context.new_page()
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self._settings.navigation_timeout_ms,
                )
                status = response.status if response is not None else None
                if status is None or status < 400:
                    await _settle(page)
                html: str = await page.content()
                return html, status
            except PWTimeoutError:
                # Capture whatever DOM we have; callers run block detection on it.
                logger.warning("navigation timeout for %s", url)
                html = await page.content()
                return html, None
            finally:
                await page.close()

        try:
            return await asyncio.wait_for(_fetch(), timeout=budget)
        except asyncio.TimeoutError as exc:
            raise BrowserDriverError(f"page fetch exceeded budget ({budget:.0f}s): {url}") from exc
        except Exception as exc:  # pragma: no cover
            logger.error("fetch failed for %s: %s", url, exc)
            raise BrowserDriverError(f"fetch failed: {exc}") from exc