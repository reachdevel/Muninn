"""Mojeek search parser."""

from __future__ import annotations

from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from app.models import SearchResult
from drivers.parsers.base import BaseParser


class MojeekParser(BaseParser):
    ENGINE_NAME = "mojeek"
    BASE_URL = "https://www.mojeek.com"

    BLOCK_SIGNATURES: tuple[str, ...] = (
        "captcha",
        "too many requests",
        "rate limit",
        "access denied",
        "cloudflare",
        "unsupported browser",
        "enable javascript",
    )

    @classmethod
    def search_url(cls, query: str, max_results: int = 10) -> str:
        # Mojeek has no region parameter that could be verified (this host is
        # currently 403'd by Mojeek), and its region is a browser cookie rather
        # than a query argument. Left as-is deliberately.
        return f"{cls.BASE_URL}/search?q={quote_plus(query)}"

    @classmethod
    def parse(cls, html: str, max_results: int = 10) -> list[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []

        # Mojeek renders each hit inside ul.results-standard > li.
        for item in soup.select("ul.results-standard li.result, ul.results-standard li"):
            link = item.select_one("a.title[href], h2 a[href], a[href]")
            if link is None:
                continue
            url = cls._abs_url(link, cls.BASE_URL)
            if not url or "mojeek.com" in url:
                continue
            snippet = cls._text(item.select_one("p.s, p"))
            title = link.get_text(" ", strip=True) or cls._text(item.select_one("h2"))
            results.append(SearchResult(title=title, url=url, snippet=snippet))
            if len(results) >= max_results:
                break

        return cls._dedupe(results)[:max_results]