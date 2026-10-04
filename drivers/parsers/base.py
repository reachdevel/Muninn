"""Base parser interface, block detection, and shared helpers.

If you are adding a new engine:
  1. Subclass ``BaseParser``.
  2. Set ``ENGINE_NAME``, ``BASE_URL``, content-local ``BLOCK_SIGNATURES``.
  3. Implement ``search_url`` and ``parse`` (use ``_text`` / ``_abs_url``).
  4. Register it in ``drivers.parsers.PARSER_REGISTRY``.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4.element import Tag

from app.models import SearchResult


def _attr(node: Tag | None, name: str) -> str:
    """Read a tag attribute as a plain string.

    BeautifulSoup types an attribute as ``str | AttributeValueList | None``
    (multi-valued attributes such as ``class`` return a list); we only want the
    scalar text form.
    """
    if node is None:
        return ""
    value = node.get(name)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return " ".join(str(part) for part in value)


class EngineBlockedError(Exception):
    """Raised when a search engine answers with 429 / CAPTCHA / block page."""

    def __init__(self, engine: str, reason: str) -> None:
        self.engine = engine
        self.reason = reason
        super().__init__(f"engine {engine!r} blocked: {reason}")


class BaseParser:
    """Base class for all search engine parsers."""

    ENGINE_NAME: str = ""
    BASE_URL: str = ""
    #: Market requested from the engine, as a two-letter country code. Set from
    #: Settings.search_region at startup by apply_search_region(); parsers build
    #: their own URL parameters from it, because each engine names the parameter
    #: differently (and some ignore region entirely).
    REGION: str = "us"
    # Case-insensitive substrings that mark a CAPTCHA / bot-block page.
    BLOCK_SIGNATURES: tuple[str, ...] = ()
    # Substrings that mark a page which *never rendered its results* (a
    # JavaScript-driven SERP captured too early, or a bot shell). Only engines
    # that set this get the empty-result guard; leaving it empty keeps the
    # existing behaviour, where zero results is a legitimate answer.
    JS_SHELL_SIGNATURES: tuple[str, ...] = ()
    #: Whether this engine returns ``site:`` results in the initial HTML.
    #:
    #: Measured live: DuckDuckGo, Brave and Ecosia do; Bing and Yahoo return a
    #: 240KB shell with no result markup at all for a ``site:`` query, rendering
    #: them client-side. Skipping them for that query shape matters because zero
    #: parsed results otherwise look like an authoritative "nothing found" and get
    #: cached for a day - on the one query type callers most need right.
    SERVES_SITE_OPERATOR: bool = True
    # Optional page to visit once in a fresh browser context, before this
    # engine's first search. Empty for most engines.
    #
    # This exists for Ecosia, whose "firewall" is a cookie wall rather than an IP
    # block: a context arriving cold at a search URL gets 403, while the same
    # context that loaded the homepage first is served normally. Verified both
    # ways against the live site, including with plain HTTP, which never gets
    # through. It is opt-in because it costs one page load per context.
    WARMUP_URL: str = ""

    # ------------------------------------------------------------------ URL
    @classmethod
    def search_url(cls, query: str, max_results: int = 10) -> str:
        """Build the engine search URL for ``query``."""
        raise NotImplementedError

    # --------------------------------------------------------------- blocks
    #: Statuses that mean the engine refused us, not that it found nothing.
    #: This mirrors the scrape path's blocked statuses (parsers.block_detector):
    #: without it a 403 page parses to zero results, and zero results is
    #: indistinguishable from an authoritative "this query has no hits" - so the
    #: refusal gets cached for a day as if it were an answer.
    BLOCKED_STATUSES: frozenset[int] = frozenset({401, 403, 429, 503})

    @classmethod
    def detect_block(cls, html: str, status: int | None = None) -> str | None:
        """Return a reason string if the page looks blocked, else ``None``.

        Any refusal status (401/403/429/503) counts, as does any CAPTCHA or
        bot-detection signature.
        """
        if status is not None and status in cls.BLOCKED_STATUSES:
            return str(status)
        lowered = (html or "").lower()
        for signature in cls.BLOCK_SIGNATURES:
            if signature in lowered:
                return "captcha"
        return None

    @classmethod
    def looks_like_js_shell(cls, html: str) -> bool:
        """Whether the page is a shell that never rendered any results.

        For a JavaScript-rendered engine this is the difference between "the
        query found nothing" and "we captured the page before it painted" - and
        the second one must never be cached as an empty result set for a day.
        """
        lowered = (html or "").lower()
        return any(signature in lowered for signature in cls.JS_SHELL_SIGNATURES)

    # -------------------------------------------------------------- parsing
    @classmethod
    def parse(cls, html: str, max_results: int = 10) -> list[SearchResult]:
        """Parse organic results out of the engine HTML."""
        raise NotImplementedError

    @staticmethod
    def _text(node: Tag | None) -> str:
        """Return collapsed inner text of a BeautifulSoup node."""
        if node is None:
            return ""
        return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()

    @staticmethod
    def _abs_url(node: Tag | None, base_url: str) -> str:
        """Resolve a possibly-relative href to an absolute URL."""
        href = _attr(node, "href")
        if not href:
            return ""
        return urljoin(base_url, href)

    @classmethod
    def _dedupe(cls, results: list[SearchResult]) -> list[SearchResult]:
        """De-duplicate by URL, preserving order."""
        seen: set[str] = set()
        unique: list[SearchResult] = []
        for r in results:
            if r.url in seen:
                continue
            seen.add(r.url)
            unique.append(r)
        return unique