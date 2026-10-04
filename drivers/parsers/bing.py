"""Bing Search parser."""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, quote_plus, urlsplit

from bs4 import BeautifulSoup

from app.models import SearchResult
from drivers.parsers.base import BaseParser


class BingParser(BaseParser):
    ENGINE_NAME = "bing"
    BASE_URL = "https://www.bing.com"

    #: Verified live: Bing renders `site:` results client-side, so the
    #: initial HTML carries no result markup and the parsed answer would be
    #: a false "nothing found" rather than an empty SERP.
    SERVES_SITE_OPERATOR = False

    BLOCK_SIGNATURES: tuple[str, ...] = (
        "captcha",
        "unusual traffic",
        "verify you're a human",
        "verify you are a human",
        "not a robot",
        "our systems have detected unusual traffic",
        "we're sorry, but your computer or network may be sending automated queries",
    )

    @classmethod
    def search_url(cls, query: str, max_results: int = 10) -> str:
        count = max(1, min(max_results, 50))
        # mkt/cc pin the market: setlang alone leaves a German exit IP on a German
        # SERP. Verified live - mkt=en-US&cc=US returns the same ten results the
        # untargeted URL does, with no German hosts in the top set.
        market = cls.REGION.upper()
        return (
            f"{cls.BASE_URL}/search?q={quote_plus(query)}"
            f"&count={count}&setlang=en&mkt=en-{market}&cc={market}"
        )

    @staticmethod
    def _unwrap(url: str) -> str:
        """Recover the destination from Bing's ``/ck/a?...&u=a1<base64>`` wrapper.

        Without this every result points at ``bing.com`` instead of the page it
        describes: the parser looks healthy - ten results, real titles - while
        handing the caller ten identical URLs. The ``u`` parameter is ``a1``
        followed by the base64url-encoded destination.
        """
        if "/ck/a" not in url:
            return url
        token = parse_qs(urlsplit(url).query).get("u", [""])[0]
        if not token.startswith("a1"):
            return url
        raw = token[2:]
        try:
            padded = raw + "=" * (-len(raw) % 4)
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return url
        return decoded if decoded.startswith("http") else url

    @classmethod
    def parse(cls, html: str, max_results: int = 10) -> list[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []

        for item in soup.select("li.b_algo"):
            link = item.select_one("h2 a[href]")
            if link is None:
                continue
            url = cls._unwrap(cls._abs_url(link, cls.BASE_URL))
            if not url or "bing.com/search" in url:
                continue
            snippet = cls._text(item.select_one("p, .b_caption p, .b_lineclamp2, .b_lineclamp3, .b_lineclamp4"))
            results.append(SearchResult(title=cls._text(item.select_one("h2")), url=url, snippet=snippet))
            if len(results) >= max_results:
                break

        return cls._dedupe(results)[:max_results]