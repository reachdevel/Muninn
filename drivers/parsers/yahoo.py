"""Yahoo Search parser.

Yahoo serves a plain server-rendered SERP with no anti-bot interstitial in front
of it, which makes it the easiest engine in this pool to scrape. Two details
matter for correctness:

* the anchor inside a result contains a *breadcrumb* ("GeeksForGeeks https://… ›
  python") as well as the title, so the title is read from ``h3.title`` rather
  than from the link text;
* the destination href is direct, not a tracking redirect, so no unwrapping is
  needed (unlike Bing's ``/ck/a?`` wrapper or DuckDuckGo's ``/l/?uddg=``).

Yahoo's *market* is tied to the exit IP and it has no region parameter that
reliably moves it; ``vl=lang_en`` pins the interface language. Expect it to behave
like Bing on a German IP.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from app.models import SearchResult
from drivers.parsers.base import BaseParser


class YahooParser(BaseParser):
    ENGINE_NAME = "yahoo"
    BASE_URL = "https://search.yahoo.com"

    #: Verified live: Yahoo renders `site:` results client-side, so the
    #: initial HTML carries no result markup and the parsed answer would be
    #: a false "nothing found" rather than an empty SERP.
    SERVES_SITE_OPERATOR = False

    BLOCK_SIGNATURES: tuple[str, ...] = (
        # Each verified absent from a healthy SERP. "captcha" is deliberately not
        # here: engines bundle UI strings, and Brave's bundle contains the word on
        # every successful page.
        "unusual traffic",
        "verify you are human",
        "are you not a robot",
        "too many requests",
        "access denied",
    )

    #: Yahoo hydrates parts of the page client-side.
    JS_SHELL_SIGNATURES: tuple[str, ...] = (
        "enable javascript",
        "noscript",
        "unusual traffic",
    )

    @classmethod
    def search_url(cls, query: str, max_results: int = 10) -> str:
        # vl pins the interface language; there is no market parameter Yahoo
        # honours, so the result set still follows the exit IP.
        return f"{cls.BASE_URL}/search?p={quote_plus(query)}&vl=lang_en"

    @classmethod
    def parse(cls, html: str, max_results: int = 10) -> list[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []

        for item in soup.select("#web div.algo, div.algo"):
            link = item.select_one("div.compTitle a[href], h3.title a[href], a[href^='http']")
            if link is None:
                continue
            url = cls._abs_url(link, cls.BASE_URL)
            if not url or "search.yahoo.com" in url or "yahoo.com/" in url:
                continue
            title = cls._text(item.select_one("h3.title")) or cls._text(link)
            snippet = cls._text(item.select_one("div.compText p, p.compText, div.compText"))
            results.append(SearchResult(title=title, url=url, snippet=snippet))
            if len(results) >= max_results:
                break

        return cls._dedupe(results)[:max_results]