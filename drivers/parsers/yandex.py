"""Yandex search parser.

Yandex is the most aggressively protected engine in the pool: it answers with a
SmartCaptcha ("Are you not a robot?") for anything that looks automated, and the
browser follows the redirect, so the parser sees the captcha page rather than a
302. The block signatures below are taken from a live response rather than
guessed - that page is what "blocked" looks like here.

The organic markup is read defensively (several selectors per field, in the order
the site has used them) because Yandex renames classes without warning. Links are
sometimes wrapped in a ``/clck/jsredir?...&to=...`` redirect, which is unwrapped.

Expect this engine to spend more time in quarantine than in rotation. That is the
circuit breaker working, not a parser bug.
"""

from __future__ import annotations

from urllib.parse import parse_qs, quote_plus, urlsplit

from bs4 import BeautifulSoup

from app.models import SearchResult
from drivers.parsers.base import BaseParser

#: ``/clck/jsredir?...&to=<urlencoded destination>``
_CLICK_REDIRECT = "/clck/jsredir?"


class YandexParser(BaseParser):
    ENGINE_NAME = "yandex"
    BASE_URL = "https://yandex.com"

    BLOCK_SIGNATURES: tuple[str, ...] = (
        # The SmartCaptcha interstitial, verbatim from a live response.
        "are you not a robot",
        "smartcaptcha",
        "showcaptcha",
        "checkboxcaptcha",
        # Generic bot walls it falls back to.
        "captcha",
        "too many requests",
        "access denied",
        "enable javascript",
    )

    #: Yandex renders its SERP with JavaScript, so an empty result set from a page
    #: that never rendered is a block, not a search that found nothing.
    JS_SHELL_SIGNATURES: tuple[str, ...] = (
        "are you not a robot",
        "enable javascript",
        "noscript",
    )

    @classmethod
    def search_url(cls, query: str, max_results: int = 10) -> str:
        # lr=84 is the English-language region hint; without it the SERP is
        # localised from the exit IP, which changes the markup we parse.
        return f"{cls.BASE_URL}/search/?text={quote_plus(query)}&lr=84"

    @staticmethod
    def _unwrap(url: str) -> str:
        """Unwrap Yandex's ``/clck/jsredir`` click-tracking redirect."""
        if _CLICK_REDIRECT not in url:
            return url
        token = parse_qs(urlsplit(url).query).get("to")
        return token[0] if token else url

    @classmethod
    def parse(cls, html: str, max_results: int = 10) -> list[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []

        # Each organic hit is a serp-item; the class has drifted between
        # "serp-item" and "serp-item_type_search" over the years.
        rows = soup.select(
            "li.serp-item, li.serp-item_type_search, .serp-list > li, .Organic"
        )
        for item in rows:
            link = item.select_one(
                "a.OrganicTitle-Link[href], "
                "a.Link.Link_theme_normal[href], "
                ".OrganicTitle a[href], "
                "a[class*='OrganicTitle'][href]"
            )
            if link is None:
                continue
            url = cls._unwrap(cls._abs_url(link, cls.BASE_URL))
            if not url or "yandex." in url or "yastatic.net" in url:
                continue
            title = cls._text(link) or cls._text(item.select_one("h2, .OrganicTitle"))
            snippet = cls._text(
                item.select_one(
                    ".OrganicTextContentSpan, "
                    ".TextContainer, "
                    ".organic__content-wrapper, "
                    ".OrganicText"
                )
            )
            results.append(SearchResult(title=title, url=url, snippet=snippet))
            if len(results) >= max_results:
                break

        # Some responses skip the item wrapper and emit a flat list of title
        # links; fall back to that rather than returning nothing.
        if not results:
            for link in soup.select("a.OrganicTitle-Link[href], a.Link_theme_normal[href]"):
                url = cls._unwrap(cls._abs_url(link, cls.BASE_URL))
                if not url or "yandex." in url or "yastatic.net" in url:
                    continue
                results.append(SearchResult(title=cls._text(link), url=url, snippet=""))
                if len(results) >= max_results:
                    break

        return cls._dedupe(results)[:max_results]