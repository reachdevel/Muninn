"""Muninn FastAPI gateway - /search, /scrape, /health, /status.

The application is long-lived: the stealth Chromium driver, SQLite cache,
engine manager, queue worker, scrape fast-path fetcher, and the supervising
browser-pool manager are all started once in the lifespan and shut down
cleanly on exit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.cache import SearchCache
from app.config import SUPPORTED_ENGINES, Settings, get_settings
from app.engine_manager import AllEnginesQuarantinedError, EngineManager
from app.engine_state_store import EngineStateStore
from app.models import SearchResponse
from app.request_context import install as install_middleware
from app.search_service import (
    ClientDisconnectedError,
    EngineQuarantinedError,
    SearchJobFailedError,
    SearchService,
    SearchUnavailableError,
)
from browser_pool.manager import BrowserPoolManager
from drivers.browser_driver import BrowserDriver, BrowserDriverError
from fetchers.fast_path import FastPathFetcher
from ops.cache import ScrapeCache
from ops.logging import configure_logging
from ops.metrics import Registry
from ops.politeness import HostPoliteness
from ops.ratelimit import RateLimiter, RateLimitExceeded
from routers.health import router as health_router
from routers.metrics import router as metrics_router
from routers.scrape import router as scrape_router
from schemas.common import ErrorResponse
from services.scrape_service import ScrapeService

logger = logging.getLogger("muninn")


def create_app(
    settings: Settings | None = None,
    driver_factory: Callable[[Settings], BrowserDriver | None] = lambda s: BrowserDriver(s),
    scrape_fetcher_factory: Callable[[Settings], FastPathFetcher] = FastPathFetcher,
    scrape_pool_factory: Callable[[Settings], BrowserPoolManager] = BrowserPoolManager,
) -> FastAPI:
    """Application factory.

    Tests inject a ``driver_factory`` (canned search HTML) plus, for the scrape
    module, ``scrape_fetcher_factory`` (mocked httpx transport) and/or
    ``scrape_pool_factory`` (fake browser pool) to stay offline and fast.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_format, settings.log_level)

    metrics = Registry()
    metrics.describe("muninn_http_requests_total", "counter", "HTTP requests served")
    metrics.describe("muninn_http_request_seconds", "histogram", "HTTP request duration")
    metrics.describe("muninn_search_queue_wait_seconds", "histogram", "Time a search waited in the queue")
    metrics.describe("muninn_search_engine_seconds", "histogram", "Search engine execution time")
    metrics.describe(
        "muninn_search_job_seconds",
        "histogram",
        "End-to-end search job duration (execution only, excludes queue wait)",
    )
    metrics.describe(
        "muninn_search_jobs_total",
        "counter",
        "Search jobs by outcome: served, quarantined, failed, deadline, abandoned",
    )
    metrics.describe(
        "muninn_search_deadline_kills_total",
        "counter",
        "Jobs killed by the per-job execution deadline, by the engine in flight",
    )
    metrics.describe(
        "muninn_search_breaker_trips_total",
        "counter",
        "Search load-breaker openings, by reason",
    )
    metrics.describe(
        "muninn_search_breaker_rejections_total",
        "counter",
        "Searches refused by the load breaker, by reason",
    )
    metrics.describe(
        "muninn_search_breaker_state",
        "gauge",
        "Load breaker: 0 closed, 1 half-open (probing), 2 open (refusing)",
    )
    metrics.describe(
        "muninn_search_worker_state",
        "gauge",
        "Worst search-worker state: 0 idle, 1 busy, 2 stuck, 3 dead",
    )
    metrics.describe("muninn_search_workers", "gauge", "Search workers in the pool")
    metrics.describe(
        "muninn_search_worker_restarts_total",
        "counter",
        "Search workers replaced by the supervisor",
    )
    metrics.describe(
        "muninn_search_queue_stuck",
        "gauge",
        "1 when the search queue is non-empty and nothing has completed recently",
    )
    metrics.describe(
        "muninn_engine_total",
        "counter",
        "Search engine attempts by engine and outcome (success, block, timeout, network, parse)",
    )
    metrics.describe("muninn_scrape_leg_seconds", "histogram", "Scrape leg duration")
    metrics.describe("muninn_scrape_total", "counter", "Scrapes by outcome")
    metrics.describe("muninn_search_queue_rejections_total", "counter", "Searches refused: queue full")
    metrics.describe("muninn_search_rate_limited_total", "counter", "Searches refused: rate limit")
    metrics.describe("muninn_scrape_rate_limited_total", "counter", "Scrapes refused: rate limit")
    metrics.describe("muninn_cache_entries", "gauge", "Live entries in the search cache")
    metrics.describe("muninn_scrape_cache_entries", "gauge", "Entries in the scrape cache")
    metrics.describe("muninn_search_queue_depth", "gauge", "Depth of the search queue")
    metrics.describe("muninn_browser_pool_jobs", "gauge", "Render jobs the browser pool has run")
    metrics.describe("muninn_browser_pool_up", "gauge", "1 when the browser pool answers")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        driver = driver_factory(settings)
        if driver is None:
            raise RuntimeError(
                "driver_factory returned no browser driver; the search API cannot start"
            )
        cache = SearchCache(settings.cache_db_path, settings.cache_ttl_seconds)
        fetch: FastPathFetcher = scrape_fetcher_factory(settings)
        pool: BrowserPoolManager = scrape_pool_factory(settings)

        try:
            await driver.start()
            await cache.connect()
            await fetch.start()
        except BrowserDriverError as exc:
            logger.error("browser failed to start: %s", exc)
            raise

        # The engine manager needs the open cache connection to make its
        # circuit-breaker state durable, and is restored before any traffic is
        # served so a restart does not re-hammer an engine that just blocked us.
        engines = EngineManager(
            settings, store=EngineStateStore(cache.connection), registry=metrics
        )
        await engines.restore()
        service = SearchService(settings, driver, cache, engines, registry=metrics)

        # --- scrape module -------------------------------------------------
        politeness = HostPoliteness(
            settings.per_host_delay_seconds,
            settings.politeness_idle_evict_seconds,
        )
        scrape_cache = ScrapeCache(settings.scrape_cache_ttl, settings.scrape_cache_max_entries)
        limiter = RateLimiter(settings.scrape_rate_limit_per_minute)
        search_limiter = RateLimiter(settings.search_rate_limit_per_minute)
        scrape_service = ScrapeService(
            settings,
            fetcher=fetch,
            politeness=politeness,
            cache=scrape_cache,
            browser_pool=pool,
            registry=metrics,
        )

        await service.start()
        await pool.start()
        maintenance = await cache.start_maintenance()

        app.state.settings = settings
        app.state.driver = driver
        app.state.cache = cache
        app.state.engines = engines
        app.state.service = service
        app.state.scrape_service = scrape_service
        app.state.scrape_pool = pool
        app.state.scrape_cache = scrape_cache
        app.state.scrape_limiter = limiter
        app.state.search_limiter = search_limiter
        app.state.metrics = metrics
        app.state.started_at = time.time()

        yield

        await service.stop()
        await pool.stop()
        maintenance.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance
        await driver.stop()
        await fetch.close()
        await cache.close()

    app = FastAPI(
        title="Muninn API Gateway",
        summary="Self-hosted web search and page scraping over a stealth browser.",
        description=(
            "Muninn exposes a small REST API in front of a persistent, "
            "anti-bot-hardened Chromium instance.\n\n"
            "* **`/search`** runs a query through a rotating pool of search "
            "engines, behind a throttling queue and a circuit breaker that "
            "quarantines engines which block us.\n"
            "* **`/scrape`** fetches an arbitrary public URL, escalating to an "
            "isolated stealth-browser process when the site challenges the "
            "request, and returns clean text, metadata and links.\n\n"
            "**This service is unauthenticated and single-user.** It binds to "
            "`127.0.0.1` by default; see the Security and Legal sections of the "
            "README before exposing it anywhere else."
        ),
        version="0.1.0",
        lifespan=lifespan,
        contact={"name": "Levent Kurt", "url": "https://github.com/reachdevel/Muninn"},
        license_info={
            "name": "MIT",
            "url": "https://github.com/reachdevel/Muninn/blob/main/LICENSE",
        },
        openapi_tags=[
            {
                "name": "search",
                "description": "Execute searches across Google, Bing, DuckDuckGo "
                "and Mojeek through a stealth browser.",
            },
            {
                "name": "scrape",
                "description": "Fetch and extract a page, escalating to a stealth "
                "browser when the site blocks the request.",
            },
            {
                "name": "health",
                "description": "Liveness, readiness and the deep health report.",
            },
            {
                "name": "status",
                "description": "Queue, cache and per-engine metrics.",
            },
        ],
        # Swagger UI is served by default. The service has no authentication, so
        # DOCS_ENABLED=0 is the switch to turn the schema off where the machine
        # is reachable by anyone else.
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    # ------------------------------------------------------------------ routes

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {
            "service": "Muninn API Gateway",
            "version": "0.1.0",
            "endpoints": {
                "/search": "GET q, max_results, engine, force_refresh",
                "/scrape": "GET url, render, max_text",
                "/health": "GET service health (fetcher + browser pool)",
                "/health/live": "GET cheap liveness probe",
                "/health/ready": "GET readiness probe",
                "/status": "GET engine + queue metrics",
                "/metrics": "GET Prometheus metrics",
                "/docs": "GET Swagger UI (DOCS_ENABLED, default on)",
                "/redoc": "GET ReDoc reference view",
                "/openapi.json": "GET the OpenAPI schema",
            },
            "engines": list(SUPPORTED_ENGINES),
        }

    @app.get(
        "/search",
        tags=["search"],
        summary="Execute a search",
        description=(
            "Runs `q` against a search engine through the stealth browser.\n\n"
            "Uncached queries join a FIFO queue drained by a small worker pool "
            "that enforces a randomized delay between outbound requests, which "
            "keeps a residential IP well below quota. Engines rotate "
            "round-robin, one job per engine at a time; an engine that answers "
            "429 or a CAPTCHA is quarantined (5 minutes, doubling per "
            "consecutive failure and capped at 30 minutes) and the query is "
            "retried on the next active engine. Pinning `engine` to a "
            "quarantined one is refused immediately rather than retried "
            "elsewhere.\n\n"
            "Every job runs under a hard deadline "
            "(`SEARCH_JOB_DEADLINE_SECONDS`), so a hung upstream call cannot pin "
            "a worker. If the queue is full, or the workers stop draining, new "
            "work is refused at once with `503` and a machine-readable reason "
            "instead of waiting; the breaker admits a single probe afterwards, "
            "so the service recovers without a restart.\n\n"
            "Successful queries are cached in SQLite for `CACHE_TTL_SECONDS` and "
            "replayed from cache on a repeat. Cached answers are never refused. "
            "`force_refresh=true` bypasses the cache in both directions: it "
            "reads through the engines and does not store the result."
        ),
        response_model=SearchResponse,
        responses={
            200: {
                "description": "Search executed (or served from cache).",
                "content": {
                    "application/json": {
                        "example": {
                            "query": "python web scraping",
                            "engine_used": "google",
                            "cached": False,
                            "execution_time_ms": 22140,
                            "results_count": 2,
                            "results": [
                                {
                                    "title": "Web Scraping - Real Python",
                                    "url": "https://realpython.com/scraping/",
                                    "snippet": "Learn how to scrape the web with Python.",
                                },
                                {
                                    "title": "Scrapy | A Fast and Powerful",
                                    "url": "https://scrapy.org/",
                                    "snippet": "An open source and collaborative framework.",
                                },
                            ],
                        }
                    }
                },
            },
            422: {"model": ErrorResponse, "description": "Invalid query parameters."},
            503: {
                "model": ErrorResponse,
                "description": "Cannot serve this search now: every engine "
                "quarantined, the pinned engine quarantined, the queue saturated, "
                "or the search path refusing work while it recovers. Always "
                "carries `Retry-After`.",
                "content": {
                    "application/json": {
                        "examples": {
                            "allEnginesQuarantined": {
                                "summary": "No engine left in the pool",
                                "value": {
                                    "error": "all_engines_quarantined",
                                    "detail": "every search engine is under quarantine; try again later",
                                },
                            },
                            "engineQuarantined": {
                                "summary": "The pinned engine is out",
                                "value": {
                                    "error": "engine_quarantined",
                                    "detail": "engine ddg is quarantined",
                                    "engine": "ddg",
                                },
                            },
                            "queueSaturated": {
                                "summary": "Queue full or not draining",
                                "value": {
                                    "error": "not_draining",
                                    "detail": "search path cannot accept work (not_draining); retry shortly",
                                    "reason": "not_draining",
                                    "queue_depth": 19,
                                    "worker_state": "stuck",
                                },
                            },
                        }
                    }
                },
            },
            504: {
                "model": ErrorResponse,
                "description": "The job ran (or waited its full turn) and did not "
                "produce results: the job deadline expired, or the search failed.",
            },
        },
    )
    async def search(
        request: Request,
        q: str = Query(..., min_length=1, max_length=500, description="Search query"),
        max_results: int = Query(10, ge=1, le=50, description="Max organic results to yield"),
        engine: str | None = Query(
            None,
            description="Engine to use. If it is quarantined the request is refused "
            "with 503 immediately - it is never silently replaced by another engine, "
            "because a caller that pinned it needs a `site:` answer, not a "
            "different one.",
            examples=["google"],
        ),
        force_refresh: bool = Query(
            False,
            description="Bypass the cache entirely: read through the engines and do "
            "not store the result.",
        ),
    ) -> SearchResponse | JSONResponse:
        if engine is not None and engine not in SUPPORTED_ENGINES:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported engine {engine!r}; choose from {list(SUPPORTED_ENGINES)}",
            )
        if engine is not None:
            engine = engine.lower()

        limiter: RateLimiter | None = getattr(request.app.state, "search_limiter", None)
        if limiter is not None:
            # Abuse protection, not authentication: the throttle already caps
            # throughput, this stops one client filling the queue.
            client = request.client.host if request.client else "unknown"
            try:
                await limiter.acquire(client)
            except RateLimitExceeded as exc:
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": str(exc.retry_after)},
                    content={
                        "error": "rate_limited",
                        "detail": str(exc),
                        "retry_after": exc.retry_after,
                    },
                )

        service: SearchService = request.app.state.service
        try:
            response = await service.submit(
                query=q,
                max_results=max_results,
                requested_engine=engine,
                force_refresh=force_refresh,
                # Best effort: stops an abandoned request from occupying a
                # worker. The per-job deadline is the guarantee, not this.
                disconnect_check=request.is_disconnected,
            )
        except AllEnginesQuarantinedError:
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "30"},
                content={
                    "error": "all_engines_quarantined",
                    "detail": "every search engine is under quarantine; try again later",
                },
            )
        except EngineQuarantinedError as exc:
            # Pinned to an engine that is out: it can never answer, so this is
            # "not now" rather than a per-job failure.
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "30"},
                content={
                    "error": "engine_quarantined",
                    "detail": f"engine {exc.engine} is quarantined",
                    "engine": exc.engine,
                },
            )
        except SearchUnavailableError as exc:
            # Breaker open, queue saturated, or a worker that would not drain.
            # Machine-readable, and fast by construction: this is the path that
            # replaces a 120-second wait.
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": str(exc.retry_after)},
                content={
                    "error": exc.reason,
                    "detail": exc.detail,
                    "reason": exc.reason,
                    "queue_depth": exc.queue_depth,
                    "worker_state": exc.worker_state,
                },
            )
        except (SearchJobFailedError, TimeoutError) as exc:
            # The job ran (or waited its full turn) and did not produce results.
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ClientDisconnectedError as exc:
            # The caller is gone; it will not read this, but a 503 keeps the
            # client's contract honest (back off and defer) if it did.
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "5"},
                content={
                    "error": "client_disconnected",
                    "detail": str(exc),
                },
            )
        return response

    @app.get(
        "/status",
        tags=["status"],
        summary="Engine, queue and cache metrics",
        description=(
            "Operational counters: how deep the search queue is, the state of the "
            "search workers and the load breaker, how many queries are cached, "
            "the per-engine circuit-breaker state (failure counts, failure "
            "class, quarantine level and remaining cooldown) and lifetime "
            "request counters."
        ),
        responses={
            200: {
                "description": "Current metrics snapshot.",
                "content": {
                    "application/json": {
                        "example": {
                            "queue_depth": 0,
                            "cached_queries_count": 42,
                            "metrics": {
                                "searches_served": 128,
                                "cache_hits": 96,
                                "cache_misses": 32,
                                "circuit_breaker_trips": 2,
                                "requests_enqueued": 32,
                                "jobs_deadline_killed": 0,
                                "breaker_rejections": 0,
                                "worker_restarts": 0,
                            },
                            "worker": {
                                "pid": 1234,
                                "state": "idle",
                                "current_job_age_s": 0.0,
                                "jobs_completed": 32,
                                "pool_size": 2,
                            },
                            "breaker": {
                                "state": "closed",
                                "reason": None,
                                "consecutive_failures": 0,
                            },
                            "engines": {
                                "google": {
                                    "status": "active",
                                    "fail_count": 0,
                                    "success_count": 12,
                                    "total_requests": 12,
                                    "quarantine_level": 0,
                                    "last_failure_class": None,
                                    "quarantined_until": None,
                                    "remaining_cooldown_seconds": 0,
                                }
                            },
                        }
                    }
                },
            }
        },
    )
    async def status(request: Request) -> dict:
        engine_mgr: EngineManager = request.app.state.engines
        service: SearchService = request.app.state.service
        cache = request.app.state.cache
        engines = await engine_mgr.status()
        return {
            "queue_depth": service.queue_depth,
            "cached_queries_count": await cache.count(),
            "metrics": service.metrics.to_dict(),
            "worker": service.worker_status(),
            "breaker": service.breaker_status(),
            "engines": engines,
        }

    app.include_router(scrape_router)
    app.include_router(health_router)
    app.include_router(metrics_router)

    # Request id, timing and the JSON 500 envelope live in app/request_context.py
    # because they have to be *pure ASGI*: Starlette's BaseHTTPMiddleware (what
    # @app.middleware("http") builds) swallows the server's http.disconnect
    # message, which silently disabled client-disconnect detection on /search.
    install_middleware(app, lambda path, status, duration: _record(metrics, path, status, duration))

    return app


#: Paths that get their own metric series. Anything else is folded into
#: "other" so an unauthenticated caller cannot create unbounded label values
#: by requesting random paths - a real risk on a scrape-able /metrics endpoint.
_KNOWN_ROUTES = frozenset(
    {
        "/",
        "/search",
        "/scrape",
        "/status",
        "/health",
        "/health/live",
        "/health/ready",
        "/metrics",
    }
)


def _record(
    registry: Registry, endpoint: str, status: int, duration: float
) -> None:
    """One HTTP request into the registry."""
    route = endpoint if endpoint in _KNOWN_ROUTES else "other"
    registry.increment(
        "muninn_http_requests_total", {"endpoint": route, "status": str(status)}
    )
    registry.observe("muninn_http_request_seconds", duration, {"endpoint": route})


def run() -> None:
    """Serve the gateway on ``Settings.host``/``Settings.port``.

    ``uvicorn app.main:app`` ignores our settings and uses its own defaults, so
    ``HOST``/``PORT`` would silently do nothing. This entry point makes the
    configured values authoritative:

        python -m app.main              # honours HOST and PORT
    """
    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


app = create_app()


if __name__ == "__main__":  # pragma: no cover - process entry point
    run()