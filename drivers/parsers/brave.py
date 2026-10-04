"""Brave Search parser.

Brave serves a server-rendered SERP: the organic results are in the initial HTML,
which makes it a good citizen of this pool. Verified against a live response, and
the classes below are the stable tokens - the ``svelte-<hash>`` suffixes are
per-build and are deliberately never referenced.

One trap worth keeping in mind: Brave ships a large localisation bundle inside the
page, and that bundle contains the words "CAPTCHA", "Cloudflare", "rate limit" and
"security check" as ordinary UI strings. Any block signature using those words
would quarantine this engine on **every** request. Every signature below was
checked against a working response and is absent from it.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from app.models import SearchResult
from drivers.parsers.base import BaseParser


class BraveParser(BaseParser):
    ENGINE_NAME = "brave"
    BASE_URL = "https://search.brave.com"

    BLOCK_SIGNATURES: tuple[str, ...] = (
        # Verified absent from a healthy SERP. See the module docstring: the
        # obvious words ("captcha", "cloudflare") are all present in one.
        "unusual traffic",
        "verify you are human",
        "are you a robot",
        "too many requests",
        "access denied",
    )

    #: Results are rendered client-side in parts, so an empty set from a page that
    #: never painted is a block rather than a search with no hits.
    JS_SHELL_SIGNATURES: tuple[str, ...] = (
        "enable javascript",
        "noscript",
        "unusual traffic",
    )

    @classmethod
    def search_url(cls, query: str, max_results: int = 10) -> str:
        # Brave honours ``country``, but only for some values. Measured live with
        # a German exit IP and the query "news": no parameter, ``country=gb``,
        # ``country=de`` and ``lang=en`` all return six German results in the top
        # ten, while ``country=us`` and ``country=all`` return none. So the code is
        # passed through as given, but any value other than us/all is silently
        # ignored and Brave falls back to geolocating by IP - worth knowing before
        # setting SEARCH_REGION to something else.
        return (
            f"{cls.BASE_URL}/search?q={quote_plus(query)}"
            f"&country={cls.REGION.lower()}&lang=en"
        )

    @classmethod
    def parse(cls, html: str, max_results: int = 10) -> list[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []

        # data-type="web" is the organic tier. Sponsored placements carry their own
        # marker and are wrapped in relative /a/redirect?click_url= links, so
        # requiring an absolute href drops them as well.
        for item in soup.select('div.snippet[data-type="web"], div.snippet[data-type="webpage"]'):
            link = item.select_one(
                ".result-content a[href^='http'], .result-wrapper a[href^='http']"
            )
            if link is None:
                continue
            url = cls._abs_url(link, cls.BASE_URL)
            if not url or "search.brave.com" in url:
                continue
            title = cls._text(
                item.select_one(
                    ".title.search-snippet-title, .search-snippet-title, .result-title"
                )
            ) or cls._text(link)
            snippet = cls._text(
                item.select_one(
                    ".generic-snippet, .snippet-description, .result-description"
                )
            )
            results.append(SearchResult(title=title, url=url, snippet=snippet))
            if len(results) >= max_results:
                break

        return cls._dedupe(results)[:max_results]