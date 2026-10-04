"""Ecosia search parser.

Two things about Ecosia are worth knowing, and both cost a wrong answer if you
miss them.

**It is a cookie wall, not an IP block.** A browser context that arrives cold at a
search URL gets HTTP 403 and an "Ecosia Firewall" page; the same context that
visited the homepage first is served a full SERP. Plain HTTP gets the firewall page
every time, whatever headers it sends. Hence ``WARMUP_URL``: the driver visits the
homepage once per context before the first search. (Verified live, both ways.)

**The display URL is not the destination.** Each result carries a visible
breadcrumb link (``a.result-info__link`` - "https://www.ionos.com › …") *and* the
real one (``a.result__link``). The breadcrumb is the same support-article link on
every single result, so selecting "any absolute link" quietly returns the same URL
ten times. The parser takes ``a.result__link`` only.

Results are provided by Google on the way through, which is worth knowing when you
are comparing them against a direct Google query.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from app.models import SearchResult
from drivers.parsers.base import BaseParser


class EcosiaParser(BaseParser):
    ENGINE_NAME = "ecosia"
    BASE_URL = "https://www.ecosia.org"

    #: Visited once per browser context before this engine's first search. Without
    #: it the firewall answers a cold context with 403.
    WARMUP_URL = BASE_URL + "/"

    BLOCK_SIGNATURES: tuple[str, ...] = (
        # Both verified present on the real firewall page *and* absent from a
        # healthy SERP. "challenge" and "enable javascript" were tried first and
        # quarantined this engine on every successful search.
        "ecosia firewall",
        "unusual traffic",
        "checking your browser",
        "too many requests",
        "access denied",
    )

    #: Parts of the SERP are rendered client-side, so an empty set from a page
    #: that never painted is a block rather than a search with no hits.
    JS_SHELL_SIGNATURES: tuple[str, ...] = (
        "ecosia firewall",
        "enable javascript",
        "noscript",
    )

    @classmethod
    def search_url(cls, query: str, max_results: int = 10) -> str:
        # No region parameter: none of locale=, addon= or a bare URL changed the
        # result set in live testing, and the browser context already sends
        # Accept-Language from Settings.locale.
        return f"{cls.BASE_URL}/search?q={quote_plus(query)}"

    @classmethod
    def parse(cls, html: str, max_results: int = 10) -> list[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []

        for item in soup.select("article.result, .web-result, .mainline__result"):
            # a.result__link is the destination; the sibling .result-info__link is
            # the breadcrumb and is the same support page on every result.
            link = item.select_one(".result__title a.result__link[href], a.result__link[href]")
            if link is None:
                continue
            url = cls._abs_url(link, cls.BASE_URL)
            if not url or "ecosia.org" in url:
                continue
            title = cls._text(item.select_one("h2.result-title__heading, .result__title"))
            snippet = cls._text(
                item.select_one(
                    "p.web-result__description, .web-result__description, "
                    ".result__description p, .result__description"
                )
            )
            results.append(SearchResult(title=title, url=url, snippet=snippet))
            if len(results) >= max_results:
                break

        return cls._dedupe(results)[:max_results]