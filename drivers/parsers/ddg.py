"""DuckDuckGo (HTML endpoint) search parser.

The lightweight ``html.duckduckgo.com/html`` endpoint renders server-side and
keeps the same DOM shape for years, which is far more headless-friendly than
the JS-heavy ``duckduckgo.com`` app. Result links are wrapped in
``//duckduckgo.com/l/?uddg=<encoded>`` redirects that must be unwrapped.
"""

from __future__ import annotations

from urllib.parse import parse_qs, quote_plus, urlsplit

from bs4 import BeautifulSoup

from app.models import SearchResult
from drivers.parsers.base import BaseParser


class DuckDuckGoParser(BaseParser):
    ENGINE_NAME = "ddg"
    BASE_URL = "https://html.duckduckgo.com/html"

    BLOCK_SIGNATURES: tuple[str, ...] = (
        "anomaly",
        # Deliberately NOT the bare word "captcha": DuckDuckGo serves ads
        # alongside results, and for a scraping query the ad it serves reads
        # "Forget about blockers with automated proxy and CAPTCHA handling".
        # Matching the bare word quarantined this engine on perfectly healthy
        # pages, which looked like the engine being blocked rather than like our
        # own detector misfiring.
        "verify you are human",
        "our systems have detected unusual traffic",
        "too many requests",
        "enable javascript",
    )

    @classmethod
    def search_url(cls, query: str, max_results: int = 10) -> str:
        # No region parameter, deliberately. DuckDuckGo's ``kl=`` looks like the
        # obvious way to pin a market, and it breaks this engine: measured live,
        # ``kl=us-en`` and ``kl=uk-en`` both answer HTTP 202 with zero results,
        # while the bare URL answers 200 with ten - reproducibly, in either
        # order, so it is the parameter and not rate limiting. Their results are
        # already region-neutral for this query, so the knob is left alone rather
        # than set to something that costs us the engine.
        return f"{cls.BASE_URL}/?q={quote_plus(query)}"

    @staticmethod
    def _unwrap(url: str) -> str:
        """Extract the real destination from a DDG ``/l/?uddg=`` redirect."""
        if "/l/?uddg=" not in url:
            return url
        token = parse_qs(urlsplit(url).query).get("uddg")
        return token[0] if token else url

    @classmethod
    def parse(cls, html: str, max_results: int = 10) -> list[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []

        for item in soup.select("div.result"):
            link = item.select_one("a.result__a[href]")
            if link is None:
                continue
            url = cls._unwrap(cls._abs_url(link, cls.BASE_URL))
            if not url or "duckduckgo.com" in url:
                continue
            snippet = cls._text(item.select_one("a.result__snippet[href], .result__snippet"))
            results.append(SearchResult(title=cls._text(link), url=url, snippet=snippet))
            if len(results) >= max_results:
                break

        return cls._dedupe(results)[:max_results]