"""Unit tests for every engine parser and unified block detection."""

from __future__ import annotations

import pytest

from app.models import SearchResult
from drivers.parsers import (
    BingParser,
    BraveParser,
    DuckDuckGoParser,
    EcosiaParser,
    GoogleParser,
    MojeekParser,
    QwantParser,
    YahooParser,
    YandexParser,
    apply_search_region,
    get_parser,
)
from drivers.parsers.base import BaseParser
from tests.html_fixtures import (
    BING_HTML,
    BING_WRAPPED_HTML,
    BLOCK_PAGES,
    BRAVE_HTML,
    DDG_HTML,
    ECOSIA_HTML,
    GOOGLE_HTML,
    MOJEEK_HTML,
    QWANT_HTML,
    QWANT_JS_SHELL_HTML,
    YAHOO_HTML,
    YANDEX_HTML,
)

#: Every parser, so a new engine joins the shared assertions automatically.
ALL_PARSERS = [
    GoogleParser,
    BingParser,
    DuckDuckGoParser,
    MojeekParser,
    YandexParser,
    QwantParser,
    YahooParser,
    BraveParser,
    EcosiaParser,
]

# --------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "parser,html",
    [
        (GoogleParser, GOOGLE_HTML),
        (BingParser, BING_HTML),
        (DuckDuckGoParser, DDG_HTML),
        (MojeekParser, MOJEEK_HTML),
        (YandexParser, YANDEX_HTML),
        (QwantParser, QWANT_HTML),
        (BraveParser, BRAVE_HTML),
        (EcosiaParser, ECOSIA_HTML),
    ],
)
def test_parse_returns_results(parser: type[BaseParser], html: str) -> None:
    results = parser.parse(html, max_results=10)
    assert results
    assert all(isinstance(r, SearchResult) for r in results)
    for r in results:
        assert r.title
        assert r.url.startswith("http")
        assert r.snippet


def test_parse_respects_max_results() -> None:
    assert len(GoogleParser.parse(GOOGLE_HTML, max_results=1)) == 1
    assert len(BingParser.parse(BING_HTML, max_results=5)) == 2


def test_parse_deduplicates_by_url() -> None:
    dup = GOOGLE_HTML + GOOGLE_HTML
    urls = [r.url for r in GoogleParser.parse(dup)]
    assert len(urls) == len(set(urls))


def test_ddg_unwraps_redirect_urls() -> None:
    live_style_html = """
    <div class="results">
      <div class="result">
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Frealpython.com%2Fbeautiful-soup-web-scraper-python%2F&rut=abc">Real Python</a>
        <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Frealpython.com%2Fbeautiful-soup-web-scraper-python%2F&rut=abc">Scrape with BeautifulSoup.</a>
      </div>
    </div>
    """
    results = DuckDuckGoParser.parse(live_style_html)
    assert len(results) == 1
    assert results[0].url == "https://realpython.com/beautiful-soup-web-scraper-python/"
    assert results[0].snippet == "Scrape with BeautifulSoup."


def test_detect_block_http_429() -> None:
    assert GoogleParser.detect_block("<html>ok</html>", status=429) == "429"


@pytest.mark.parametrize(
    "parser,html",
    [
        (GoogleParser, BLOCK_PAGES["google"]),
        (BingParser, BLOCK_PAGES["bing"]),
        (DuckDuckGoParser, BLOCK_PAGES["ddg"]),
        (MojeekParser, BLOCK_PAGES["mojeek"]),
        (YandexParser, BLOCK_PAGES["yandex"]),
        (QwantParser, BLOCK_PAGES["qwant"]),
        (BraveParser, BLOCK_PAGES["ecosia"]),
        (EcosiaParser, BLOCK_PAGES["ecosia"]),
    ],
)
def test_detect_block_captcha(parser: type[BaseParser], html: str) -> None:
    assert parser.detect_block(html) is not None


def test_detect_block_clean_page() -> None:
    for parser in ALL_PARSERS:
        assert parser.detect_block(GOOGLE_HTML) is None


def test_search_urls_are_formatted() -> None:
    assert "q=python" in GoogleParser.search_url("python", 10)
    assert "python" in BingParser.search_url("python", 10)
    assert "python" in DuckDuckGoParser.search_url("python", 10)
    assert "python" in MojeekParser.search_url("python", 10)
    assert "python" in YandexParser.search_url("python", 10)
    assert "python" in QwantParser.search_url("python", 10)
    assert "python" in BraveParser.search_url("python", 10)
    assert "python" in EcosiaParser.search_url("python", 10)


def test_registry_returns_known_parsers() -> None:
    assert get_parser("google") is GoogleParser
    assert get_parser("bing") is BingParser
    assert get_parser("ddg") is DuckDuckGoParser
    assert get_parser("mojeek") is MojeekParser
    assert get_parser("yandex") is YandexParser
    assert get_parser("qwant") is QwantParser
    assert get_parser("brave") is BraveParser
    assert get_parser("ecosia") is EcosiaParser


def test_registry_rejects_unknown_engine() -> None:
    with pytest.raises(KeyError):
        get_parser("altavista")  # not a real engine

# ----------------------------------------------------------------- yandex / qwant


def test_yandex_unwraps_click_redirects() -> None:
    """Yandex wraps some outbound links in /clck/jsredir?...&to=<encoded>."""
    html = """
    <li class="serp-item"><div class="Organic">
      <h2><a class="OrganicTitle-Link"
             href="https://yandex.com/clck/jsredir?from=yandex.com&amp;to=https%3A%2F%2Fexample.org%2Fpage">Example</a></h2>
      <div class="TextContainer"><span class="OrganicTextContentSpan">A hit.</span></div>
    </div></li>
    """
    results = YandexParser.parse(html)
    assert len(results) == 1
    assert results[0].url == "https://example.org/page"


def test_yandex_drops_its_own_links() -> None:
    html = """
    <li class="serp-item"><div class="Organic">
      <h2><a class="OrganicTitle-Link" href="https://yandex.com/images/search?x=1">Images</a></h2>
    </div></li>
    """
    assert YandexParser.parse(html) == []


def test_yandex_falls_back_to_a_flat_link_list() -> None:
    """Some responses omit the serp-item wrapper entirely."""
    html = (
        '<a class="OrganicTitle-Link" href="https://example.com/a">A</a>'
        '<a class="OrganicTitle-Link" href="https://example.com/b">B</a>'
    )
    results = YandexParser.parse(html)
    assert [r.url for r in results] == ["https://example.com/a", "https://example.com/b"]


def test_qwant_skips_its_own_chrome_links() -> None:
    html = """
    <div data-testid="containerWeb">
      <a href="https://about.qwant.com/en/">About</a>
      <article><a href="https://example.com/x"><h2>Hit</h2></a><p>Body.</p></article>
    </div>
    """
    results = QwantParser.parse(html)
    assert [r.url for r in results] == ["https://example.com/x"]


def test_a_javascript_shell_is_recognised_as_a_block_not_as_no_hits() -> None:
    """The empty-SERP trap.

    Both new engines render client-side, so a capture can land before the results
    paint. Treating that as "this query found nothing" would cache a wrong answer
    for a day and report a successful search, so the shell is detected and
    charged to the engine as a failure instead.
    """
    assert QwantParser.parse(QWANT_JS_SHELL_HTML) == []
    assert QwantParser.looks_like_js_shell(QWANT_JS_SHELL_HTML) is True
    assert YandexParser.looks_like_js_shell(BLOCK_PAGES["yandex"]) is True


def test_a_rendered_page_is_not_mistaken_for_a_shell() -> None:
    for parser, html in ((QwantParser, QWANT_HTML), (YandexParser, YANDEX_HTML)):
        assert parser.looks_like_js_shell(html) is False
        assert parser.parse(html)


def test_the_shell_guard_is_opt_in() -> None:
    """Engines that serve server-rendered HTML keep the old behaviour.

    A genuinely empty SERP from google or bing is a valid answer, not a fault,
    and must not quarantine the engine.
    """
    for parser in (GoogleParser, BingParser, DuckDuckGoParser, MojeekParser):
        assert parser.looks_like_js_shell("<html><body>no hits</body></html>") is False


# --------------------------------------------------------------- brave specifics


def test_brave_ignores_its_localisation_bundle_when_looking_for_blocks() -> None:
    """The trap that would have broken this engine on every request.

    Brave ships a large localisation bundle inside the page, and it contains the
    words "CAPTCHA", "Cloudflare", "rate limit" and "security check" as ordinary
    UI strings. A conventional block-signature list would therefore classify a
    perfectly good SERP as blocked. This asserts the safe-signature rule against
    the real working-page text.
    """
    for word in ("captcha", "cloudflare", "rate limit", "security check"):
        assert word not in BraveParser.BLOCK_SIGNATURES, (
            f"{word!r} appears in a healthy Brave response; using it as a block "
            f"signature would quarantine the engine every time"
        )
    assert BraveParser.detect_block(BRAVE_HTML) is None


def test_brave_parses_real_markup_and_ignores_the_per_build_classes() -> None:
    results = BraveParser.parse(BRAVE_HTML)
    assert [r.url for r in results] == [
        "https://www.scrapy.org/",
        "https://docs.python.org/3/",
    ]
    assert results[0].title.startswith("Scrapy")
    assert "leading open source Python framework" in results[0].snippet
    assert results[0].snippet


def test_brave_drops_sponsored_results() -> None:
    """Ads live in relative /a/redirect?click_url= links, not absolute ones."""
    urls = [r.url for r in BraveParser.parse(BRAVE_HTML)]
    assert not any("upwork" in u or "click_url" in u for u in urls)


def test_brave_respects_max_results() -> None:
    assert len(BraveParser.parse(BRAVE_HTML, max_results=1)) == 1


# -------------------------------------------------------------- ecosia specifics


def test_ecosia_reads_its_result_markup() -> None:
    results = EcosiaParser.parse(ECOSIA_HTML)
    assert [r.url for r in results] == [
        "https://www.ionos.com/guide/web-scraping-with-python",
        "https://example.org/scraping",
    ]
    assert results[0].title == "Web scraping with Python"
    assert results[0].snippet == "Here you will learn how to scrape websites using Python."


def test_ecosia_uses_the_destination_link_not_the_visible_breadcrumb() -> None:
    """The trap that made an early version of this parser useless.

    Every result carries a visible breadcrumb link (``a.result-info__link``) that
    is the *same* support-article URL on all of them. Selecting "any absolute
    link" therefore returns that one URL ten times instead of ten destinations.
    """
    urls = [r.url for r in EcosiaParser.parse(ECOSIA_HTML)]
    assert "https://support.ecosia.org/article/579" not in urls
    assert len(set(urls)) == len(urls)


def test_ecosia_warms_its_context_up() -> None:
    """Its wall is a cookie wall: a cold context on a search URL gets 403."""
    assert EcosiaParser.WARMUP_URL == "https://www.ecosia.org/"
    # Opt-in: engines without a warm-up must not pay for one.
    for parser in (GoogleParser, BingParser, DuckDuckGoParser, MojeekParser, BraveParser):
        assert parser.WARMUP_URL == ""


def test_ecosia_firewall_page_is_recognised() -> None:
    """The 403 body it actually served this host."""
    assert EcosiaParser.detect_block(BLOCK_PAGES["ecosia"]) is not None
    assert EcosiaParser.parse(BLOCK_PAGES["ecosia"]) == []


# --------------------------------------------------------------- yahoo specifics


def test_yahoo_reads_real_markup_and_takes_the_title_from_the_heading() -> None:
    """The anchor text is a breadcrumb ("Scrapy scrapy.org"), not the title."""
    results = YahooParser.parse(YAHOO_HTML)
    assert [r.url for r in results] == [
        "https://www.scrapy.org/",
        "https://docs.python.org/3/library/venv.html",
    ]
    assert results[0].title == "Scrapy, a fast high-level web crawling framework"
    assert results[0].snippet.startswith("Scrapy is a fast high-level")


def test_yahoo_has_no_block_signatures_a_healthy_page_contains() -> None:
    """Verified against a real response: none of these appear in a good SERP."""
    assert YahooParser.detect_block(YAHOO_HTML) is None
    assert YahooParser.detect_block(BLOCK_PAGES["yahoo"]) is not None


# ----------------------------------------------------------------- bing unwrap


def test_bing_unwraps_its_click_tracker_to_the_real_destination() -> None:
    """The bug this fixes.

    Bing wraps every destination in ``/ck/a?...&u=a1<base64>``. Parsing without
    unwrapping produces ten results that all point at bing.com - full marks on
    "did it return results", and completely wrong answers.
    """
    results = BingParser.parse(BING_WRAPPED_HTML)
    assert [r.url for r in results] == [
        "https://docs.python.org/3/",
        "https://realpython.com/",
    ]
    assert all("bing.com" not in r.url for r in results)


def test_bing_leaves_a_direct_url_alone() -> None:
    assert BingParser._unwrap("https://example.com/x") == "https://example.com/x"  # noqa: SLF001


def test_bing_unwrap_survives_a_malformed_tracker() -> None:
    for bad in ("https://www.bing.com/ck/a?u=notbase64!!", "https://www.bing.com/ck/a?u=a1"):
        assert BingParser._unwrap(bad) == bad  # noqa: SLF001


# ---------------------------------------------------------------- search region


def test_the_region_reaches_the_engines_that_honour_it() -> None:
    apply_search_region("gb")
    try:
        assert "gl=gb" in GoogleParser.search_url("x", 10)
        assert "mkt=en-GB" in BingParser.search_url("x", 10)
        assert "cc=GB" in BingParser.search_url("x", 10)
        assert "country=gb" in BraveParser.search_url("x", 10)
    finally:
        apply_search_region("us")


def test_the_region_is_applied_to_every_parser_including_parked_ones() -> None:
    apply_search_region("de")
    try:
        for parser in ALL_PARSERS:
            assert parser.REGION == "de"
    finally:
        apply_search_region("us")


@pytest.mark.parametrize("region", ["", "germany", "u1", "12", "usa"])
def test_a_nonsense_region_is_refused_rather_than_sent_to_six_engines(region: str) -> None:
    with pytest.raises(ValueError):
        apply_search_region(region)


def test_duckduckgo_is_never_given_a_region_parameter() -> None:
    """`kl=` looks like the obvious fix for German results and costs us the engine.

    Measured live: ``kl=us-en`` and ``kl=uk-en`` both answer HTTP 202 with zero
    results, while the bare URL answers 200 with ten - reproducibly, in either
    order. So the region knob is left alone here on purpose.
    """
    for url in (DuckDuckGoParser.search_url("x", 10),):
        assert "kl=" not in url
        assert url.endswith("q=x")


def test_region_neutral_urls_are_unchanged_by_the_knob() -> None:
    """Mojeek and Ecosia have no region parameter that could be verified."""
    apply_search_region("jp")
    try:
        for parser in (MojeekParser, EcosiaParser):
            assert parser.search_url("x", 10) == parser.search_url("x", 10)
    finally:
        apply_search_region("us")


def test_the_app_applies_the_configured_region_at_startup() -> None:
    """Wiring: one setting, applied before any traffic is served."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import FakeDriver, make_test_settings

    with TestClient(
        create_app(
            settings=make_test_settings(search_region="gb"),
            driver_factory=lambda s: FakeDriver(),
        )
    ) as client:
        assert client.get("/health/live").status_code == 200
    assert GoogleParser.REGION == "gb"
    apply_search_region("us")


# ------------------------------------------------------------ site: operator support


def test_which_engines_can_serve_site_queries_server_side() -> None:
    """The split, from live measurements.

    Bing and Yahoo render `site:` results client-side: the initial HTML has no
    result markup at all, so asking them yields a shell that parses to nothing.
    """
    assert BingParser.SERVES_SITE_OPERATOR is False
    assert YahooParser.SERVES_SITE_OPERATOR is False
    for parser in (GoogleParser, DuckDuckGoParser, MojeekParser, BraveParser, EcosiaParser):
        assert parser.SERVES_SITE_OPERATOR is True, parser.__name__


def test_site_operator_detection() -> None:
    from app.search_service import _uses_site_operator  # noqa: PLC0415

    assert _uses_site_operator("site:example.com")
    assert _uses_site_operator("python site:docs.python.org scraping")
    assert _uses_site_operator("inurl:login admin")
    assert _uses_site_operator("SITE:Example.COM")
    assert not _uses_site_operator("site examples")
    assert not _uses_site_operator("websites for scraping")
    assert not _uses_site_operator("")


# ------------------------------------------------- block detection hardening


def test_a_refusal_status_is_a_block_not_an_empty_answer() -> None:
    """A 403 page must never be cached as "this query has no hits".

    Mojeek answered this host with 403, the parser found nothing in it, and the
    empty result looked exactly like an authoritative answer — which the service
    then cached for a day. Every refusal status is a block now.
    """
    for parser in ALL_PARSERS:
        for status in (401, 403, 429, 503):
            assert parser.detect_block("<html><body>refused</body></html>", status) == str(status), (
                f"{parser.__name__} treats HTTP {status} as an empty result"
            )
        # A 200 with no content is not a block; that is a real empty SERP.
        assert parser.detect_block("", 200) is None
        assert parser.detect_block("<html></html>", 404) is None


def test_ddg_is_not_blocked_by_its_own_advertisements() -> None:
    """The live false positive, preserved as a regression test.

    DuckDuckGo serves ads next to results, and for a scraping query the ad says
    "Forget about blockers with automated proxy and CAPTCHA handling". Matching
    the bare word quarantined a working engine on every healthy page.
    """
    ad = (
        '<div class="result"><a class="result__a" href="https://realpython.com/">Real Python</a></div>'
        "<aside>Forget about blockers with automated proxy and CAPTCHA handling. "
        "Reduce time to data.</aside>"
    )
    assert DuckDuckGoParser.detect_block(ad, 200) is None
    assert DuckDuckGoParser.parse(ad)[0].url == "https://realpython.com/"
    # Its real block page is still caught, by the specific phrase.
    assert DuckDuckGoParser.detect_block(BLOCK_PAGES["ddg"], 200) is not None
