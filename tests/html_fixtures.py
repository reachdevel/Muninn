"""Shared sample HTML used by parser tests, API tests, and integration tests."""

GOOGLE_HTML = """
<div id="search"><div id="rso">
  <div class="g">
    <a href="https://docs.python.org/3/"><h3>Python 3 Documentation</h3></a>
    <div class="VwiC3b">Official docs for the Python language.</div>
  </div>
  <div class="g">
    <a href="https://www.python.org/"><h3>Python.org</h3></a>
    <div class="VwiC3b">The official home of the Python Programming Language.</div>
  </div>
</div></div>
"""

BING_HTML = """
<ol id="b_results">
  <li class="b_algo">
    <h2><a href="https://en.wikipedia.org/wiki/Python_(programming_language)">Python (programming language)</a></h2>
    <p class="b_lineclamp4">Python is a high-level, interpreted programming language.</p>
  </li>
  <li class="b_algo">
    <h2><a href="https://example.com/python">Python Tutorial</a></h2>
    <p>Learn Python from scratch.</p>
  </li>
</ol>
"""

#: Bing answers with every destination wrapped in a ``/ck/a?...&u=a1<base64>``
#: click tracker. Parsing it without unwrapping yields ten results that all point
#: at bing.com - which looks perfectly healthy and is entirely wrong.
BING_WRAPPED_HTML = """
<ol id="b_results">
  <li class="b_algo">
    <h2><a href="https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly9kb2NzLnB5dGhvbi5vcmcvMy8=&ntb=1">Python 3 documentation</a></h2>
    <p>Official docs.</p>
  </li>
  <li class="b_algo">
    <h2><a href="https://www.bing.com/ck/a?!&&p=def&u=a1aHR0cHM6Ly9yZWFscHl0aG9uLmNvbS8">Real Python</a></h2>
    <p>Tutorials.</p>
  </li>
</ol>
"""

DDG_HTML = """
<div class="results">
  <div class="result">
    <a class="result__a" href="https://docs.python.org/3/tutorial/">The Python Tutorial</a>
    <a class="result__snippet" href="https://docs.python.org/3/tutorial/">An informal introduction to Python.</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://realpython.com/">Real Python</a>
    <div class="result__snippet">Tutorials for professional developers.</div>
  </div>
</div>
"""

MOJEEK_HTML = """
<ul class="results-standard">
  <li class="result standard">
    <h2><a class="title" href="https://www.mojeek.com/about/">About Mojeek</a></h2>
    <p class="s">Independent search engine with its own index.</p>
  </li>
  <li class="result standard">
    <h2><a class="title" href="https://example.org/py">Python on Example</a></h2>
    <p class="s">A Python overview page.</p>
  </li>
</ul>
"""

YANDEX_HTML = """
<ul class="serp-list">
  <li class="serp-item serp-item_type_search">
    <div class="Organic">
      <h2><a class="Link Link_theme_normal OrganicTitle-Link"
             href="https://docs.python.org/3/">Python 3 documentation</a></h2>
      <div class="TextContainer"><span class="OrganicTextContentSpan">The official Python language documentation.</span></div>
    </div>
  </li>
  <li class="serp-item">
    <div class="Organic">
      <h2><a class="Link Link_theme_normal OrganicTitle-Link"
             href="/clck/jsredir?from=yandex.com&amp;to=https%3A%2F%2Frealpython.com%2F">Real Python</a></h2>
      <div class="TextContainer"><span class="OrganicTextContentSpan">Tutorials for working with Python.</span></div>
    </div>
  </li>
</ul>
"""

QWANT_HTML = """
<div data-testid="containerWeb" id="main-content">
  <article class="web-result">
    <a href="https://docs.python.org/3/"><h2 class="result-title">Python 3 documentation</h2></a>
    <p class="result-description">The official Python language documentation.</p>
  </article>
  <article class="web-result">
    <a href="https://realpython.com/"><h2 class="result-title">Real Python</h2></a>
    <p class="result-description">Tutorials for working with Python.</p>
  </article>
</div>
"""

#: Modelled on a real Brave Search response, including the per-build ``svelte-``
#: class suffixes (which the parser must not rely on) and a sponsored block.
BRAVE_HTML = """
<div class="snippet" data-pos="1" data-type="web" data-keynav="true">
  <div class="result-body svelte-1rq4ngz">
    <div class="result-wrapper svelte-1rq4ngz">
      <div class="result-content svelte-1rq4ngz">
        <a href="https://www.scrapy.org/" target="_self" class="svelte-14r20fy l1">
          <div class="site-name-wrapper svelte-on1hvy"><div class="url-wrapper">scrapy.org</div></div>
          <div class="title search-snippet-title line-clamp-1 svelte-1rq4ngz">Scrapy - open source web scraping framework</div>
        </a>
        <div class="generic-snippet svelte-1cwdgg3"><div class="content desktop-default-regular t-primary line">Scrapy is the leading open source Python framework for web scraping.</div></div>
      </div>
    </div>
  </div>
</div>
<div class="snippet" data-pos="2" data-type="web">
  <div class="result-body svelte-1rq4ngz"><div class="result-wrapper svelte-1rq4ngz">
    <div class="result-content svelte-1rq4ngz">
      <a href="https://docs.python.org/3/" target="_self" class="svelte-14r20fy l1">
        <div class="title search-snippet-title line-clamp-1">Python 3 documentation</div>
      </a>
      <div class="generic-snippet"><div class="content">The official Python language documentation.</div></div>
    </div>
  </div></div>
</div>
<div class="snippet" data-type="ad">
  <div class="result-wrapper"><div class="result-content">
    <a href="/a/redirect?click_url=https%3A%2F%2Fwww.upwork.com%2Fhire&amp;placement_id=abc">Sponsored: hire a data scientist</a>
  </div></div>
</div>
"""

#: Modelled on a real warm Ecosia response. Note ``result-info__link``: the visible
#: breadcrumb, which is the same support-article URL on *every* result, next to
#: the real destination in ``a.result__link``.
#: Modelled on a real Yahoo response, including the breadcrumb text that lives
#: inside the result anchor (so the title has to come from h3.title).
YAHOO_HTML = """
<div id="web">
  <div class="dd fst algo algo-sr relsrch richAlgo">
    <div class="compTitle options-toggle">
      <a class="d-ib va-top mxw-100p" href="https://www.scrapy.org/">
        <span class="d-ib va-mid">Scrapy</span>
      </a>
      <h3 class="title fc-2015C2-imp pt-6 ivmt-6 mxw-100p">Scrapy, a fast high-level web crawling framework</h3>
    </div>
    <div class="compText aAbs">
      <p class="fc-dustygray fz-14 lh-22 ls-02 mah-44 ov-h d">Scrapy is a fast high-level web crawling and scraping framework.</p>
    </div>
  </div>
  <div class="dd algo">
    <div class="compTitle">
      <a class="d-ib va-top" href="https://docs.python.org/3/library/venv.html"><span class="d-ib va-mid">docs.python.org</span></a>
      <h3 class="title">venv - Virtual environments for Python</h3>
    </div>
    <div class="compText"><p class="fc-dustygray fz-14">The venv module provides a lightweight way to create isolated environments.</p></div>
  </div>
</div>
"""

ECOSIA_HTML = """
<div class="results">
  <article class="result web-result mainline__result">
    <div class="result__body">
      <div class="result__header">
        <div class="result__info result__info--extended">
          <div class="result-info result-info--complex">
            <div class="result-info__name">IONOS</div>
            <div class="result-info__link-container">
              <a class="result-info__link" href="https://support.ecosia.org/article/579">https://www.ionos.com &#x203A; guide</a>
            </div>
          </div>
        </div>
        <div class="result__title">
          <a class="result__link link link--as-a" href="https://www.ionos.com/guide/web-scraping-with-python"><h2 class="result-title__heading">Web scraping with Python</h2></a>
        </div>
      </div>
      <div class="result__columns">
        <div class="result__description"><p class="web-result__description">Here you will learn how to scrape websites using Python.</p></div>
      </div>
    </div>
  </article>
  <article class="result web-result mainline__result">
    <div class="result__body">
      <div class="result__header">
        <div class="result__info result-info--complex">
          <div class="result-info__link-container">
            <a class="result-info__link" href="https://support.ecosia.org/article/579">https://example.org &#x203A; scraping</a>
          </div>
        </div>
        <div class="result__title">
          <a class="result__link link link--as-a" href="https://example.org/scraping"><h2 class="result-title__heading">Scraping from scratch</h2></a>
        </div>
      </div>
      <div class="result__columns">
        <div class="result__description"><p class="web-result__description">A hands-on guide.</p></div>
      </div>
    </div>
  </article>
</div>
"""

#: A Qwant app shell captured before the results painted: no organic markup, and
#: the markers that say "nothing rendered here".
QWANT_JS_SHELL_HTML = """
<html><head><title>python - Qwant</title></head>
<body><noscript>Please enable JavaScript to run this app.</noscript>
<div id="main-content"><div id="item-0"></div><div id="item-1"></div></div>
<script type="module" src="/assets/client-BFSsQxgK.js"></script>
<img src="https://www.qwant.com/qwant-logo-seo.9d058972.png">
</body></html>
"""

# Blocked pages keyed by engine name.
BLOCK_PAGES = {
    "google": """
      <html><body>
        <form id="captcha-form"><div class="g-recaptcha"></div></form>
        <p>Our systems have detected unusual traffic from your computer network.</p>
      </body></html>
    """,
    "bing": "<html><body><h1>Sorry, please verify you are not a robot</h1></body></html>",
    "ddg": "<html><body><h1>Anomaly detected - please solve the captcha</h1></body></html>",
    "mojeek": "<html><body><h1>Too many requests - rate limit exceeded</h1></body></html>",
    # Captured from a live request, not invented.
    "yandex": (
        "<html><body><h1>Are you not a robot?</h1>"
        '<div id="checkbox-captcha"></div>'
        '<script src="https://yastatic.net/captcha/checkbox-captcha.js"></script>'
        "</body></html>"
    ),
    "qwant": (
        "<html><body><div id='ddChallengeContainer1791066717337'></div>"
        '<script src="https://ct.captcha-delivery.com/c.js"></script>'
        "</body></html>"
    ),
    "yahoo": (
        "<html><body><h1>Unusual traffic</h1>"
        "<p>We have detected unusual traffic from your network.</p></body></html>"
    ),
    # Brave never blocked a live probe, so this is synthetic - built only from
    # signatures verified absent from a healthy response (see the parser's
    # docstring: "captcha" and "cloudflare" would match its own JS bundle).
    "brave": (
        "<html><body><h1>Verify you are human</h1>"
        "<p>Please continue to search.</p></body></html>"
    ),
    # The literal 403 body Ecosia served this host.
    "ecosia": (
        "<html><head><title>Ecosia Firewall</title></head><body>"
        "<h1>Ecosia Firewall</h1><p>Your request looks unusual.</p>"
        "<p>unusual traffic</p></body></html>"
    ),
}

# Successful result HTML keyed by engine name.
ENGINE_RESULTS_HTML = {
    "google": GOOGLE_HTML,
    "bing": BING_HTML,
    "ddg": DDG_HTML,
    "mojeek": MOJEEK_HTML,
    "yandex": YANDEX_HTML,
    "qwant": QWANT_HTML,
    "brave": BRAVE_HTML,
    "ecosia": ECOSIA_HTML,
    "yahoo": YAHOO_HTML,
}