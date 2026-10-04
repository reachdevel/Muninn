"""Qwant search parser.

Qwant is a JavaScript single-page app: the server returns an empty shell and the
results are rendered client-side. That works here (the driver is a real browser),
but it also means the capture can land *before* the results paint - or on a bot
challenge instead. A live request from a datacentre IP came back as a DataDome
interstitial (``ddChallengeContainer``), which is what the block signatures below
match.

Selectors are written in tiers because the app's class names are hashed per
build. If a future build renames them, this engine degrades to a ``js-shell``
block and drops out of rotation rather than returning an empty result set: a
challenge page must never be cached as "this query has no hits".
"""

from __future__ import annotations

from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from app.models import SearchResult
from drivers.parsers.base import BaseParser


class QwantParser(BaseParser):
    ENGINE_NAME = "qwant"
    BASE_URL = "https://www.qwant.com"

    BLOCK_SIGNATURES: tuple[str, ...] = (
        # DataDome interstitial, captured from a live response.
        "ddchallengecontainer",
        "ddstylecaptcha",
        "captcha-delivery",
        # Generic bot walls and rate limits.
        "captcha",
        "too many requests",
        "rate limit",
        "access denied",
        "enable javascript",
        "unusual traffic",
    )

    #: The SERP is painted client-side; no results plus any of these means the
    #: page never rendered rather than that the query found nothing.
    JS_SHELL_SIGNATURES: tuple[str, ...] = (
        "enable javascript",
        "noscript",
        "ddchallengecontainer",
        "qwant-logo-seo",
    )

    @classmethod
    def search_url(cls, query: str, max_results: int = 10) -> str:
        return f"{cls.BASE_URL}/?q={quote_plus(query)}&t=web"

    @classmethod
    def parse(cls, html: str, max_results: int = 10) -> list[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []

        # Tiers, most specific first. The test ids are stable across builds;
        # the hashed classes are not, so they are tried last.
        rows = soup.select(
            '[data-testid="containerWeb"] article, '
            "#main-content article, "
            '[data-testid="webResult"], '
            "article"
        )
        for item in rows:
            link = item.select_one('a[href^="http"]')
            if link is None:
                continue
            url = cls._abs_url(link, cls.BASE_URL)
            if not url or "qwant.com" in url:
                continue
            title = (
                cls._text(item.select_one("h2, h3, [class*='result'] a, a"))
                or cls._text(link)
            )
            snippet = cls._text(item.select_one("p, [class*='description'], [class*='snippet']"))
            results.append(SearchResult(title=title, url=url, snippet=snippet))
            if len(results) >= max_results:
                break

        if not results:
            # Fallback: any outbound link under the results container that is not
            # one of Qwant's own chrome links.
            for link in soup.select("#main-content a[href^='http'], [data-testid='containerWeb'] a[href^='http']"):
                url = cls._abs_url(link, cls.BASE_URL)
                if not url or "qwant.com" in url:
                    continue
                results.append(SearchResult(title=cls._text(link), url=url, snippet=""))
                if len(results) >= max_results:
                    break

        return cls._dedupe(results)[:max_results]