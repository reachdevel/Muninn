"""Google Search parser."""

from __future__ import annotations

from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from app.models import SearchResult
from drivers.parsers.base import BaseParser


class GoogleParser(BaseParser):
    ENGINE_NAME = "google"
    BASE_URL = "https://www.google.com"

    BLOCK_SIGNATURES: tuple[str, ...] = (
        "unusual traffic",           # https://www.google.com/sorry/ consent blocks
        "g-recaptcha",
        "recaptcha",
        "enable javascript and cookies",
        "not a robot",
        "our systems have detected unusual traffic",
        # The other wall Google shows. The basic-HTML view (gbv=1) answers a
        # 33KB consent interstitial titled "Before you continue to Google Search"
        # rather than the usual captcha - a 33KB page with no result markup, so
        # without this it parsed to nothing and would have been cached as an
        # authoritative "no results".
        "before you continue to google search",
    )

    @classmethod
    def search_url(cls, query: str, max_results: int = 10) -> str:
        count = max(1, min(max_results, 50))
        # gl pins the *country*, not just the language: a German exit IP otherwise
        # gets a German SERP even with hl=en. (Unverifiable from this host, which
        # Google challenges, but both parameters are Google's documented API.)
        region = cls.REGION.lower()
        return (
            f"{cls.BASE_URL}/search?q={quote_plus(query)}"
            f"&num={count}&hl=en&gl={region}&pws=0"
        )

    @classmethod
    def parse(cls, html: str, max_results: int = 10) -> list[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []

        # Google's organic results live in #search/#rso; the exact container
        # class changes over time so we accept several known wrappers.
        containers = soup.select(
            "div#search div.g, div#rso div.g, div#search div.MjjYud, "
            "div#rso div.MjjYud, div#search div[data-snc]"
        )

        for container in containers:
            title_node = container.find("h3")
            link = container.find("a", href=True)
            if title_node is None or link is None:
                continue
            url = cls._abs_url(link, cls.BASE_URL)
            if not url or url.startswith(cls.BASE_URL + "/search") or "/aclk?" in url or url.endswith("/url?"):
                continue
            snippet = cls._text(container.select_one("div.VwiC3b, div[data-sncf], div.aCOpRe"))
            results.append(SearchResult(title=cls._text(title_node), url=url, snippet=snippet))
            if len(results) >= max_results:
                break

        return cls._dedupe(results)[:max_results]