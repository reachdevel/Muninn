"""Health and status endpoints.

Monitors and reports status for BOTH sides of the gateway:

  * the search side: stealth search driver, engine rotation, queue, cache; and
  * the scrape side: the HTTP fast-path fetcher and the isolated Stealth
    Browser process pool (worker state, context concurrency, idle timers).

Three endpoints, deliberately, because they cost very different amounts:

``/health/live``
    Process liveness, plus the search worker and the load breaker: pid, state,
    in-flight job age and job counters. Still no I/O, because a healthcheck
    polls it every few seconds - but it is no longer blind, which is how a dead
    worker with a populated queue could look exactly like a healthy idle one.

``/health/ready``
    Readiness from in-process state only: is the browser up and is at least one
    search engine usable? No probes, so a load balancer can use it. Note that
    readiness deliberately does *not* follow the breaker: taking the instance out
    of rotation while it refuses would starve the half-open probe that closes
    it, which is precisely how a breaker latches forever.

``/health``
    The full picture, including a live worker probe and cache counts. For a
    human or a dashboard, not for a 5-second heartbeat.

A lazy-starting subprocess worker that has simply not been used yet is a healthy
state; only an unreachable *external* worker degrades readiness.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

from app.search_breaker import CLOSED

if TYPE_CHECKING:
    from browser_pool.manager import PoolStatus

router = APIRouter(tags=["health"])


def _verdict(active: list[str], pool_status: PoolStatus, breaker_state: str) -> str:
    if not active:
        return "degraded"
    if breaker_state != CLOSED:
        # The search path is refusing work while it recovers. /scrape is
        # unaffected, so this is a partial fault, not an outage.
        return "degraded"
    if pool_status.mode == "external" and not pool_status.ok:
        return "degraded"
    return "ok"


@router.get(
    "/health",
    summary="Deep health report",
    description=(
        "Reports engine quarantine, queue depth, cache size, scrape-cache "
        "occupancy, rate-limiter state and the browser pool. Performs a live "
        "worker probe and a database query, so prefer `/health/live` for a "
        "heartbeat."
    ),
    responses={
        200: {
            "description": "Full health report.",
            "content": {
                "application/json": {
                    "example": {
                        "status": "ok",
                        "browser_ready": True,
                        "queue_depth": 0,
                        "cache_entries": 42,
                        "active_engines": ["google", "bing", "ddg", "mojeek", "yandex", "qwant"],
                        "quarantined_engines": [],
                        "uptime_seconds": 3600,
                        "worker": {
                            "pid": 1234,
                            "state": "idle",
                            "current_job_age_s": 0.0,
                            "jobs_completed": 32,
                            "pool_size": 2,
                        },
                        "breaker": {"state": "closed", "reason": None},
                        "fetcher": {
                            "status": "ok",
                            "engine": "httpx-fast-path",
                            "max_body_bytes": 10_000_000,
                            "per_host_delay": 2.0,
                        },
                        "scrape_cache": {"entries": 12, "evictions": 0},
                        "rate_limiter": {"tracked_clients": 1, "per_minute": 60},
                        "browser_pool": {
                            "status": "ok",
                            "mode": "subprocess",
                            "detail": "lazy (worker idle-exited)",
                            "browser_started": False,
                            "active_contexts": 0,
                            "max_contexts": 1,
                            "jobs": 3,
                            "idle_seconds": 12.5,
                        },
                    }
                }
            },
        }
    },
)
async def health(request: Request) -> dict[str, Any]:
    """Deep check: engine pool, worker probe, cache size, queue depth."""
    settings = request.app.state.settings
    engine_mgr = request.app.state.engines
    service = request.app.state.service
    driver = request.app.state.driver
    cache = request.app.state.cache
    pool = request.app.state.scrape_pool

    active = engine_mgr.active_engines()
    pool_status = await pool.status()
    scrape_cache = request.app.state.scrape_cache
    limiter = request.app.state.scrape_limiter
    breaker = service.breaker_status()

    return {
        "status": _verdict(active, pool_status, str(breaker["state"])),
        "browser_ready": driver is not None and driver.is_started,
        "queue_depth": service.queue_depth,
        "cache_entries": await cache.count(),
        "active_engines": active,
        "quarantined_engines": [e for e in engine_mgr.engines if e not in active],
        "uptime_seconds": int(time.time() - request.app.state.started_at),
        "worker": service.worker_status(),
        "breaker": breaker,
        "fetcher": {
            "status": "ok",
            "engine": "httpx-fast-path",
            "max_body_bytes": settings.scrape_max_body_bytes,
            "per_host_delay": settings.per_host_delay_seconds,
        },
        "scrape_cache": {
            "entries": scrape_cache.size(),
            "evictions": scrape_cache.evictions,
        },
        "rate_limiter": {
            "tracked_clients": limiter.tracked_clients,
            "per_minute": settings.scrape_rate_limit_per_minute,
        },
        "browser_pool": {
            "status": "ok" if pool_status.ok else "down",
            "mode": pool_status.mode,
            "detail": pool_status.detail,
            "browser_started": pool_status.browser_started,
            "active_contexts": pool_status.active_contexts,
            "max_contexts": pool_status.max_contexts,
            "jobs": pool_status.jobs,
            "idle_seconds": pool_status.idle_seconds,
        },
    }


@router.get(
    "/health/live",
    summary="Liveness probe",
    description="Cheap process liveness: no database query and no worker probe. "
    "This is the endpoint the container healthcheck polls. It carries the search "
    "worker (pid, state, in-flight job age, jobs completed) and the load breaker, "
    "so a wedged search path shows up here instead of only in /search timings.",
    responses={
        200: {
            "description": "The process is up.",
            "content": {
                "application/json": {
                    "example": {
                        "status": "ok",
                        "uptime_seconds": 3600,
                        "queue_depth": 19,
                        "worker": {
                            "pid": 1234,
                            "state": "stuck",
                            "current_job_age_s": 214.0,
                            "jobs_completed": 1470,
                            "jobs_killed": 3,
                            "restarts": 0,
                            "pool_size": 2,
                        },
                        "breaker": {
                            "state": "open",
                            "reason": "not_draining",
                            "open_for_seconds": 12.5,
                        },
                    }
                }
            },
        }
    },
)
async def health_live(request: Request) -> dict[str, Any]:
    """Cheap liveness for container healthchecks: no I/O beyond the process."""
    service = request.app.state.service
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - request.app.state.started_at),
        "queue_depth": service.queue_depth,
        "worker": service.worker_status(),
        "breaker": service.breaker_status(),
    }


@router.get(
    "/health/ready",
    summary="Readiness probe",
    description="Answers whether traffic should be routed here, using in-process "
    "state only (browser up, at least one usable search engine). No I/O. The "
    "search load breaker is reported but deliberately does not affect readiness: "
    "an instance pulled out of rotation while it refuses work would never get "
    "the probe that closes the breaker.",
    responses={
        200: {
            "description": "Readiness verdict.",
            "content": {
                "application/json": {
                    "example": {
                        "status": "ok",
                        "ready": True,
                        "active_engines": ["google", "bing", "ddg", "mojeek", "yandex", "qwant"],
                        "breaker": {"state": "closed", "reason": None},
                    }
                }
            },
        }
    },
)
async def health_ready(request: Request) -> dict[str, Any]:
    """Readiness from in-process state, without the deep probes."""
    engine_mgr = request.app.state.engines
    driver = request.app.state.driver
    service = request.app.state.service
    active = engine_mgr.active_engines()
    ready = bool(active) and driver is not None and driver.is_started
    return {
        "status": "ok" if ready else "degraded",
        "ready": ready,
        "active_engines": active,
        # Reported, not enforced - see the endpoint description.
        "breaker": service.breaker_status(),
    }
