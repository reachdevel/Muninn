"""Async search pipeline: FIFO queue, worker pool, load breaker, supervisor.

Design:

* Every non-cached request becomes a :class:`SearchJob` with an ``asyncio.Future``
  that the API endpoint awaits.
* A small pool of workers drains the FIFO queue, enforcing a randomized 15-30
  second delay between consecutive outbound requests (``random.uniform``), which
  keeps a home residential IP safely below quota. More than one worker exists so
  a hanging engine can only occupy one of them.
* Each job is executed against engines resolved by the :class:`EngineManager`
  (Round-Robin across active engines, honouring a per-engine concurrency cap).
  429/CAPTCHA blocks quarantine an engine and the job retries on the next
  active one; if none remain the future resolves with
  :class:`AllEnginesQuarantinedError` (surfaced as HTTP 503).

Three things in here exist because the search path used to wedge completely while
``/health`` and ``/scrape`` stayed fast, and nothing reported a fault:

* **A hard per-job deadline** (``SEARCH_JOB_DEADLINE_SECONDS``). One hung
  upstream call can no longer pin a worker: the job is aborted, the engine that
  was in flight is charged one failure, and the worker moves on.
* **Worker supervision.** A worker that dies is restarted and the queue entries
  it left behind are failed rather than left to rot. Previously a worker
  resolving a future whose caller had already given up raised
  ``InvalidStateError`` *inside the worker's own error handler*, killing the only
  worker; the queue then stopped draining forever while every probe answered 200.
* **A load breaker** (:mod:`app.search_breaker`) that refuses new work in
  milliseconds instead of letting it queue behind a worker that cannot drain,
  and that admits a single probe request afterwards so it recovers by itself.

The throttle wait deliberately sits *outside* the job deadline: it is our own
politeness delay, bounded by ``THROTTLE_MAX_DELAY``, and folding it into the
execution budget would cut the fetch time to almost nothing. The invariant that
matters is checked at startup - ``throttle_max_delay + job deadline`` must stay
below ``REQUEST_TIMEOUT_SECONDS``, so every job resolves before the caller runs
out of patience.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from time import perf_counter

from app.cache import SearchCache
from app.config import Settings
from app.engine_manager import AllEnginesQuarantinedError, EngineManager
from app.models import SearchResponse
from app.search_breaker import (
    REASON_QUEUE_FULL,
    REASON_QUEUE_WAIT_TIMEOUT,
    Admission,
    SearchBreaker,
)
from drivers.browser_driver import BrowserDriver
from drivers.parsers import EngineBlockedError, get_parser
from ops.metrics import Registry

logger = logging.getLogger(__name__)

#: Worker states, mirrored by the ``muninn_search_worker_state`` gauge.
WORKER_IDLE = "idle"
WORKER_BUSY = "busy"
WORKER_THROTTLED = "throttled"
WORKER_STUCK = "stuck"
WORKER_DEAD = "dead"

WORKER_GAUGE_VALUES: dict[str, int] = {
    WORKER_IDLE: 0,
    WORKER_BUSY: 1,
    WORKER_THROTTLED: 2,
    WORKER_STUCK: 3,
    WORKER_DEAD: 4,
}

# Aggregate worker state, worst first: a dead worker outranks a stuck one, that
# outranks a busy one, and a throttled worker outranks an idle one. The order
# matters for the dispatcher's health check: "the queue is not empty and every
# worker is idle" is only a deadlock if throttling is reported as its own state.
_WORKER_SEVERITY = (WORKER_DEAD, WORKER_STUCK, WORKER_BUSY, WORKER_THROTTLED, WORKER_IDLE)

# Refusal reasons that are about the queue rather than the breaker's opinion.
_QUEUE_REJECTIONS = frozenset({REASON_QUEUE_FULL, REASON_QUEUE_WAIT_TIMEOUT})


@dataclass
class SearchJob:
    query: str
    max_results: int
    requested_engine: str | None
    force_refresh: bool
    future: asyncio.Future = field(default_factory=asyncio.Future)
    enqueued_at: float | None = None
    #: Engine currently being fetched from, so a deadline kill can charge the
    #: failure to the engine that was actually in flight.
    engine_in_flight: str | None = None
    #: Set when the caller gave up (its own read timeout, or a disconnect). The
    #: worker skips these instead of burning a worker slot on dead work.
    abandoned: bool = False
    #: The task running this job, so abandonment can cancel it mid-flight.
    task: asyncio.Task | None = None
    #: The worker holding this job, so the throttle wait can be reported.
    worker: _Worker | None = None
    #: Monotonic time execution began (after the throttle), which is what the
    #: caller's queue-wait deadline is measured against.
    started_at: float | None = None


@dataclass
class Metrics:
    """Lightweight counters surfaced by the /status endpoint."""

    searches_served: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    circuit_breaker_trips: int = 0
    requests_enqueued: int = 0
    queue_rejections: int = 0
    cache_write_skips: int = 0
    breaker_rejections: int = 0
    jobs_deadline_killed: int = 0
    jobs_queue_wait_timed_out: int = 0
    jobs_abandoned: int = 0
    jobs_failed: int = 0
    worker_restarts: int = 0

    def to_dict(self) -> dict:
        return {
            "searches_served": self.searches_served,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "circuit_breaker_trips": self.circuit_breaker_trips,
            "requests_enqueued": self.requests_enqueued,
            "queue_rejections": self.queue_rejections,
            "cache_write_skips": self.cache_write_skips,
            "breaker_rejections": self.breaker_rejections,
            "jobs_deadline_killed": self.jobs_deadline_killed,
            "jobs_queue_wait_timed_out": self.jobs_queue_wait_timed_out,
            "jobs_abandoned": self.jobs_abandoned,
            "jobs_failed": self.jobs_failed,
            "worker_restarts": self.worker_restarts,
        }


class SearchUnavailableError(Exception):
    """Search work cannot be accepted right now (breaker open, queue stuck).

    Distinct from a job failure: nothing ran, so the honest answer is "I can't
    serve this now" - HTTP 503 with a machine-readable reason - rather than the
    504 a job that actually executed and failed gets.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        queue_depth: int = 0,
        worker_state: str = WORKER_IDLE,
        retry_after: int = 1,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.queue_depth = queue_depth
        self.worker_state = worker_state
        self.retry_after = retry_after


class SearchQueueFullError(SearchUnavailableError):
    """The search queue is at capacity; the caller should retry later.

    Throughput is capped at roughly 2-4 requests/minute by the throttle, so an
    unbounded queue only ever accumulates work that will time out. Refusing is
    honest and keeps one client's burst from consuming the whole backlog.
    """


class EngineQuarantinedError(Exception):
    """The caller pinned an engine that is under quarantine.

    The request can never succeed on that engine, so it is refused immediately
    instead of being queued for a timeout: a client pinning ``engine=ddg`` for a
    ``site:`` query needs to know the engine is out, not to wait two minutes for
    an answer that was never going to come.
    """

    def __init__(self, engine: str) -> None:
        super().__init__(f"engine {engine} is quarantined")
        self.engine = engine


class SearchJobFailedError(Exception):
    """A job ran and failed (deadline, driver error, unparseable response).

    The job actually executed, so this is a 504: the client spends no budget and
    carries on with the next query.
    """


class ClientDisconnectedError(Exception):
    """The caller closed the connection before the search finished.

    Best-effort resource protection, not a guarantee: the job deadline is what
    makes the worker self-healing. This only lets an abandoned request stop
    consuming a worker sooner.
    """


@dataclass
class _Worker:
    """One queue-draining worker plus the liveness we report about it."""

    index: int
    task: asyncio.Task[None]
    state: str = WORKER_IDLE
    jobs_completed: int = 0
    jobs_killed: int = 0
    restarts: int = 0
    job_started_at: float | None = None

    def begin(self, now: float) -> None:
        self.state = WORKER_BUSY
        self.job_started_at = now

    def throttle(self) -> None:
        """Holding a job, deliberately waiting for the throttle slot."""
        if self.state != WORKER_STUCK:
            self.state = WORKER_THROTTLED

    def execute(self) -> None:
        """Holding a job and fetching from an engine now."""
        if self.state != WORKER_STUCK:
            self.state = WORKER_BUSY

    def finish(self, now: float) -> None:
        self.state = WORKER_IDLE
        self.job_started_at = None

    def to_dict(self, now: float) -> dict[str, object]:
        return {
            "index": self.index,
            "state": self.state,
            "current_job_age_s": (
                round(now - self.job_started_at, 3) if self.job_started_at else 0.0
            ),
            "jobs_completed": self.jobs_completed,
            "jobs_killed": self.jobs_killed,
            "restarts": self.restarts,
        }


class SearchService:
    """Owns the request queue, worker pool, throttler and breaker."""

    def __init__(
        self,
        settings: Settings,
        driver: BrowserDriver,
        cache: SearchCache,
        engines: EngineManager,
        registry: Registry | None = None,
    ) -> None:
        self._settings = settings
        self._driver = driver
        self._cache = cache
        self._engines = engines
        self._registry = registry

        # Bounded: see SearchQueueFullError.
        self._queue: asyncio.Queue[SearchJob] = asyncio.Queue(
            maxsize=max(1, settings.max_search_queue)
        )
        self._workers: dict[int, _Worker] = {}
        self._monitor_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._last_fetch_started: float | None = None
        self._throttle_lock = asyncio.Lock()

        self._breaker = SearchBreaker(settings, registry=registry)
        self._last_progress_at = time.monotonic()
        # Jobs currently in flight per engine. Plain dict mutations: everything
        # runs on one event loop and no await happens inside the critical
        # sections, so a lock would only add a cancellation hazard.
        self._inflight: dict[str, int] = {}

        self.metrics = Metrics()

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._stopping = False
        self._warn_if_deadline_races_the_caller()
        for index in range(max(1, self._settings.search_worker_count)):
            self._spawn_worker(index)
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name="search-supervisor"
        )

    async def stop(self) -> None:
        self._stopping = True
        tasks: list[asyncio.Task[None]] = [w.task for w in self._workers.values()]
        if self._monitor_task is not None:
            tasks.append(self._monitor_task)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - shutdown must not raise
                logger.warning("search worker shutdown error", exc_info=True)
        # Anything still queued dies with the process: fail it explicitly so a
        # caller waiting during shutdown gets an error rather than a hang.
        self._drain_queue(
            SearchUnavailableError(
                "worker_died", "the service is shutting down", retry_after=1
            )
        )
        for worker in self._workers.values():
            worker.state = WORKER_DEAD
        self._monitor_task = None

    def _warn_if_deadline_races_the_caller(self) -> None:
        """Every job must resolve before the caller's read timeout fires.

        The client and Muninn both cap a search at ``REQUEST_TIMEOUT_SECONDS``
        The client and Muninn both cap a search at ``REQUEST_TIMEOUT_SECONDS`` and
        race for the finish line, so a caller cannot tell "slow" from "dead". A
        job's worst case is its queue wait - which includes the throttle - plus
        its execution; if that can reach the caller's timeout, the budgets are
        wrong and the operator should hear about it at startup rather than in an
        incident.
        """
        queue_wait = float(self._settings.search_queue_wait_deadline_seconds)
        job_deadline = float(self._settings.search_job_deadline_seconds)
        worst = queue_wait + job_deadline
        if worst >= float(self._settings.request_timeout_seconds):
            logger.warning(
                "queue wait (%.0fs) + job deadline (%.0fs) can reach "
                "REQUEST_TIMEOUT_SECONDS (%.0fs): callers will give up before the "
                "worker does. Lower SEARCH_QUEUE_WAIT_DEADLINE_SECONDS or "
                "SEARCH_JOB_DEADLINE_SECONDS, or raise REQUEST_TIMEOUT_SECONDS.",
                queue_wait, job_deadline, self._settings.request_timeout_seconds,
            )

    def _spawn_worker(self, index: int) -> None:
        existing = self._workers.get(index)
        task = asyncio.create_task(self._worker_loop(index), name=f"search-worker-{index}")
        if existing is None:
            self._workers[index] = _Worker(index=index, task=task)
        else:
            existing.task = task
            existing.state = WORKER_IDLE
            existing.job_started_at = None
            existing.restarts += 1
        logger.info("search worker %d started", index)

    # -- introspection ------------------------------------------------------

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def breaker(self) -> SearchBreaker:
        return self._breaker

    def worker_state(self) -> str:
        """Worst state across the pool: ``dead`` > ``stuck`` > ``busy`` > ``idle``."""
        if not self._workers:
            return WORKER_DEAD
        states = {w.state for w in self._workers.values()}
        for candidate in _WORKER_SEVERITY:
            if candidate in states:
                return candidate
        return WORKER_IDLE

    def worker_status(self) -> dict[str, object]:
        """Worker identity, state, in-flight job age and counters.

        Shaped for ``/health/live``, which is polled every few seconds, so it
        reads in-process state only - no database, no probe, no awaiting.
        """
        now = time.monotonic()
        workers = list(self._workers.values())
        ages = [
            now - w.job_started_at for w in workers if w.job_started_at is not None
        ]
        return {
            # The queue is drained by tasks inside this process, so the identity
            # that matters for supervision is the process they live in.
            "pid": os.getpid(),
            "state": self.worker_state(),
            "current_job_age_s": round(max(ages), 3) if ages else 0.0,
            "jobs_completed": sum(w.jobs_completed for w in workers),
            "jobs_killed": sum(w.jobs_killed for w in workers),
            "restarts": sum(w.restarts for w in workers),
            "pool_size": len(workers),
            "workers": [w.to_dict(now) for w in workers],
        }

    def breaker_status(self) -> dict[str, object]:
        return self._breaker.snapshot()

    @property
    def queue_stuck(self) -> bool:
        """Whether the queue is non-empty and nothing has completed recently.

        This is the wedge's signature, so it is readable from more than the
        supervisor: the gauge and ``/status`` both report it.
        """
        return self._queue_stuck(self._queue.qsize())

    # -- public API ---------------------------------------------------------

    async def submit(
        self,
        query: str,
        max_results: int,
        requested_engine: str | None,
        force_refresh: bool,
        disconnect_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> SearchResponse:
        """Serve ``query`` from cache or enqueue it for live execution.

        Returns a fully-formed :class:`SearchResponse`. Raises
        :class:`EngineQuarantinedError` for a pinned engine that is out,
        :class:`AllEnginesQuarantinedError` when no engine is available,
        :class:`SearchUnavailableError` when the breaker or the queue refuses the
        work, and :class:`TimeoutError` when the caller waited longer than
        ``request_timeout_seconds``.

        ``disconnect_check`` is polled while waiting (best effort): when it says
        the client is gone the job is abandoned so it stops occupying a worker.
        The job deadline is the actual guarantee.
        """
        started = time.perf_counter()

        # A cached answer is free and must stay free: it never touches the
        # queue, so a saturated search path never refuses it.
        if not force_refresh:
            entry = await self._cache.get(query)
            if entry is not None:
                self.metrics.cache_hits += 1
                self.metrics.searches_served += 1
                return SearchResponse(
                    query=query,
                    engine_used=entry.engine_used,
                    cached=True,
                    execution_time_ms=int((time.perf_counter() - started) * 1000),
                    results_count=len(entry.results),
                    results=entry.results[:max_results],
                )

        self.metrics.cache_misses += 1

        # A pinned-but-quarantined engine can never answer, so say so now rather
        # than queue a request whose only possible outcome is a timeout.
        if requested_engine is not None and not self._engines.is_available(requested_engine):
            raise EngineQuarantinedError(requested_engine)

        # Fast-fail when the entire pool is quarantined (avoids queue backlog).
        if not self._engines.active_engines():
            raise AllEnginesQuarantinedError()

        # The breaker is the only thing standing between a saturated queue and
        # another two-minute wait. Pure in-memory state: this answers in
        # microseconds, which is the whole point of a fast-fail.
        self._refresh_worker_ages()
        admission = self._breaker.admit(queue_depth=self._queue.qsize())
        if not admission.admitted:
            raise self._refusal(admission)

        job = SearchJob(
            query=query,
            max_results=max_results,
            requested_engine=requested_engine,
            force_refresh=force_refresh,
            # Monotonic, not perf_counter: this anchors deadlines that are
            # compared against the event loop's clock.
            enqueued_at=time.monotonic(),
        )
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull as exc:
            raise self._refusal(
                Admission(
                    admitted=False, reason=REASON_QUEUE_FULL, state=self._breaker.state
                )
            ) from exc
        self.metrics.requests_enqueued += 1

        try:
            response = await self._await_job(job, disconnect_check)
        except BaseException:
            # Timeout, cancellation, disconnect: nobody is waiting on this job
            # any more, so stop working on it. A job the worker already settled
            # (say, every engine was blocked) is left alone - it is finished, not
            # abandoned.
            if not job.future.done():
                self._abandon(job)
            raise
        self.metrics.searches_served += 1
        response.execution_time_ms = int((time.perf_counter() - started) * 1000)
        return response

    async def _await_job(
        self,
        job: SearchJob,
        disconnect_check: Callable[[], Awaitable[bool]] | None,
    ) -> SearchResponse:
        """Wait for the worker to settle ``job``, or give up on our own terms.

        Two deadlines, because they mean different things to the caller:

        * the **queue-wait deadline** - the job has not begun executing by now, so
          it never will in time. That is a 503: nothing ran, and the answer is
          "come back later", not a failure to report.
        * the **request timeout** - the job has had its whole turn and still has
          not produced an answer. That is a 504.

        The future is awaited through a shield on purpose: ``wait_for`` cancels
        the awaitable it is given on timeout, and that cancellation used to reach
        the worker, which then died trying to resolve a future nobody could
        receive any more.
        """
        timeout = max(1.0, float(self._settings.request_timeout_seconds))
        wait_budget = max(0.0, float(self._settings.search_queue_wait_deadline_seconds))
        # Absolute monotonic deadline, not a duration: this is compared against
        # the clock, so it has to be a point in time.
        already_waited = time.monotonic() - (job.enqueued_at or time.monotonic())
        start_deadline = (
            time.monotonic() + max(0.0, wait_budget - already_waited)
            if wait_budget
            else None
        )
        request_deadline = time.monotonic() + timeout
        waiter = asyncio.shield(job.future)
        try:
            while True:
                now = time.monotonic()
                if job.started_at is not None:
                    # Execution started, so the queue-wait deadline has served
                    # its purpose and the job deadline governs from here.
                    start_deadline = None
                elif start_deadline is not None and now >= start_deadline:
                    # Nothing has even begun: this is a refusal, not a failure.
                    self.metrics.jobs_queue_wait_timed_out += 1
                    if self._registry is not None:
                        self._registry.increment("muninn_search_queue_wait_timeouts_total")
                    raise self._refusal(
                        Admission(
                            admitted=False,
                            reason=REASON_QUEUE_WAIT_TIMEOUT,
                            state=self._breaker.state,
                        )
                    )
                remaining = request_deadline - now
                if remaining <= 0:
                    raise TimeoutError("search timed out in the queue")
                # Poll often enough to notice a start, a disconnect and the
                # queue-wait deadline promptly. All three are in-memory checks.
                done, _ = await asyncio.wait({waiter}, timeout=min(0.5, remaining))
                if done:
                    # May raise the job's own error; that is the caller's answer.
                    return waiter.result()
                if disconnect_check is not None and await disconnect_check():
                    raise ClientDisconnectedError(
                        "client disconnected before the search finished"
                    )
        finally:
            waiter.cancel()

    def _abandon(self, job: SearchJob) -> None:
        """Mark the job unwanted and stop the worker wasting time on it."""
        if not job.abandoned:
            job.abandoned = True
            self.metrics.jobs_abandoned += 1
        task = job.task
        if task is not None and not task.done():
            # Cancels the in-flight fetch (or makes the worker skip a job it has
            # not picked up yet) without touching the worker itself.
            task.cancel()

    def _refusal(self, admission: Admission) -> SearchUnavailableError:
        """Build, count and return the fast 503 for work we will not take."""
        reason = admission.reason or "breaker_open"
        self._count_refusal(reason)
        detail = {
            REASON_QUEUE_FULL: (
                f"search queue is full ({self._queue.maxsize} requests); retry shortly"
            ),
            "breaker_open": "search path is not accepting work; retry shortly",
            "probe_in_flight": "search path is recovering; retry shortly",
        }.get(reason, f"search path cannot accept work ({reason}); retry shortly")
        retry_after = self._breaker.retry_after()
        if reason == REASON_QUEUE_FULL:
            retry_after = max(retry_after, 5)
        error = SearchUnavailableError
        cls = SearchQueueFullError if reason == REASON_QUEUE_FULL else error
        return cls(
            reason,
            detail,
            queue_depth=self._queue.qsize(),
            worker_state=self.worker_state(),
            retry_after=retry_after,
        )

    def _count_refusal(self, reason: str) -> None:
        if reason in _QUEUE_REJECTIONS:
            self.metrics.queue_rejections += 1
        else:
            self.metrics.breaker_rejections += 1
        if self._registry is None:
            return
        if reason in _QUEUE_REJECTIONS:
            # Unlabelled on purpose: this series means exactly one thing, and it
            # predates the reason label. The breaker's {reason} series carries
            # the detail.
            self._registry.increment("muninn_search_queue_rejections_total")

    # -- workers ------------------------------------------------------------

    async def _worker_loop(self, index: int) -> None:
        """Drain the queue, one job at a time, under the hard job deadline.

        The worker takes a job, *immediately* records itself as holding it, and
        only then starts the job's task. The throttle wait used to happen here,
        between the dequeue and the job, which made a worker waiting its turn
        look idle while the queue was full: a healthy throttle-bound queue was
        indistinguishable from a dead dispatcher, and a job abandoned during that
        window could not be cancelled. Everything from the dequeue on now belongs
        to the job.
        """
        worker = self._workers[index]
        while True:
            job = await self._queue.get()
            try:
                if job.abandoned:
                    # The caller already gave up. Skipping it is what keeps
                    # abandoned requests from consuming the worker.
                    continue
                now = time.monotonic()
                worker.begin(now)
                self._last_progress_at = now
                job.worker = worker
                await self._run_job(worker, job)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - one bad job, not the worker
                logger.exception("search worker %d hit an unexpected error", index)
            finally:
                job.worker = None
                worker.finish(time.monotonic())
                self._queue.task_done()

    async def _run_job(self, worker: _Worker, job: SearchJob) -> None:
        """Run one job to a definitive outcome and settle its future.

        Returns normally whatever happens: a worker that raises here dies, and a
        dead worker is exactly how the queue stopped draining in the first place.
        """
        deadline = max(1.0, float(self._settings.search_job_deadline_seconds))
        task = asyncio.ensure_future(self._execute(job))
        job.task = task
        started = time.monotonic()
        self._observe_queue_wait(job, started)

        response: SearchResponse | None = None
        error: BaseException | None = None
        outcome = "served"
        try:
            # shield() so the deadline (and a client disconnect) cancel the
            # *job*, never the worker that is awaiting it.
            response = await asyncio.wait_for(asyncio.shield(task), timeout=deadline)
        except asyncio.TimeoutError:
            outcome = "deadline"
            error = SearchJobFailedError(f"search exceeded its {deadline:.0f}s job deadline")
            await self._kill_job(task)
            await self._charge_deadline(job)
        except asyncio.CancelledError:
            # Either this job was abandoned (submit cancelled its task) or the
            # worker itself is shutting down. Only the second ends the loop.
            outcome = "abandoned"
            if not task.done():
                task.cancel()
            if self._stopping:
                raise
        except AllEnginesQuarantinedError as exc:
            outcome = "quarantined"
            error = exc
            self.metrics.circuit_breaker_trips += 1
        except Exception as exc:  # pragma: no cover - defensive
            outcome = "failed"
            error = SearchJobFailedError(f"search failed: {exc}")
            logger.warning("job failed for query=%r: %s", job.query, exc)
        finally:
            now = time.monotonic()
            self._last_progress_at = now
            if outcome == "served":
                worker.jobs_completed += 1
            elif outcome == "deadline":
                worker.jobs_killed += 1
            if outcome in {"served", "quarantined", "failed"}:
                # The worker picked the job up and released it, so the path is
                # draining whatever the job's own outcome was. This is also the
                # only thing that closes a half-open breaker.
                self._breaker.note_job_completed()
            self._observe_job(now - started, outcome)
            if outcome != "abandoned":
                self._settle(job, response=response, error=error)

    async def _kill_job(self, task: asyncio.Task) -> None:
        """Abort a job that blew its deadline and wait for it to actually stop."""
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    async def _charge_deadline(self, job: SearchJob) -> None:
        """A deadline kill is a real failure, charged to the engine in flight."""
        engine = job.engine_in_flight
        if engine:
            # "job-deadline" classifies as a timeout, so the engine gets a short
            # cooldown rather than a block-length one.
            await self._engines.report_failure(engine, "job-deadline")
        self.metrics.jobs_deadline_killed += 1
        if self._registry is not None:
            self._registry.increment(
                "muninn_search_deadline_kills_total", {"engine": engine or "none"}
            )
        logger.warning(
            "search job exceeded its deadline (engine=%s query=%r); worker released",
            engine or "none", job.query,
        )
        self._breaker.note_failure("job_deadline")

    def _settle(
        self,
        job: SearchJob,
        *,
        response: SearchResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Resolve the caller's future, exactly once."""
        if job.future.done():
            return
        if error is not None:
            if isinstance(error, SearchJobFailedError):
                self.metrics.jobs_failed += 1
            job.future.set_exception(error)
        elif response is not None:
            job.future.set_result(response)
        else:
            return
        if job.abandoned and error is not None:
            # Nobody is left to receive this. Retrieving the exception stops
            # asyncio logging "exception was never retrieved" for every
            # abandoned request.
            job.future.exception()

    async def _throttle(self, job: SearchJob) -> None:
        """Wait a randomized 15-30s since the previous outbound fetch.

        Serialised by a lock, deliberately: with one worker the spacing was free,
        but with a pool two workers would otherwise both find the gap elapsed and
        fire at the same instant, doubling the instantaneous outbound rate - the
        exact thing the throttle exists to prevent. Only the *wait* is locked; the
        fetch itself happens after the lock is released, so a hanging engine
        cannot hold up anyone else's outbound request.

        The wait counts against the caller's queue-wait deadline: a job sitting in
        the throttle is not making progress, however healthy the reason is.
        """
        worker = job.worker
        if worker is not None:
            worker.throttle()
        async with self._throttle_lock:
            delay = random.uniform(
                self._settings.throttle_min_delay, self._settings.throttle_max_delay
            )
            now = time.monotonic()
            if self._last_fetch_started is not None:
                elapsed = now - self._last_fetch_started
                remaining = delay - elapsed
                if remaining > 0:
                    logger.debug("throttling %.1fs until next outbound request", remaining)
                    await asyncio.sleep(remaining)
            self._last_fetch_started = time.monotonic()
        if worker is not None:
            worker.execute()

    async def _execute(self, job: SearchJob) -> SearchResponse:
        """Run the job against active engines until one succeeds.

        The throttle wait is the first thing in here, not something the worker
        did before creating this task. Two reasons: a job that is waiting for its
        turn is holding a worker and must say so, and a caller that gives up
        during the wait must be able to cancel it.
        """
        await self._throttle(job)
        job.started_at = time.monotonic()
        engine_pool_size = len(self._engines.engines)
        for _ in range(engine_pool_size):
            engine = await self._reserve_engine(job.requested_engine)
            job.engine_in_flight = engine
            parser = get_parser(engine)
            url = parser.search_url(job.query, job.max_results)
            try:
                started = perf_counter()
                html, status = await self._driver.fetch_html(url, engine)
                if self._registry is not None:
                    self._registry.observe(
                        "muninn_search_engine_seconds",
                        perf_counter() - started,
                        {"engine": engine},
                    )
                if html is None:
                    # Navigation failed with nothing to parse - treat it exactly
                    # like a block so the engine is quarantined and we rotate.
                    raise EngineBlockedError(engine, "empty-response")
                block_reason = parser.detect_block(html, status)
                if block_reason is not None:
                    raise EngineBlockedError(engine, block_reason)

                results = parser.parse(html, job.max_results)
                await self._engines.report_success(engine)
                job.engine_in_flight = None
                if job.force_refresh:
                    # force_refresh means "do not touch the cache": read through
                    # the engines and do not store the result either. Storing it
                    # would make the flag name a lie and would let a
                    # refresh-seeded entry outlive the caller's intent.
                    self.metrics.cache_write_skips += 1
                    logger.info("query=%r force_refresh: result not cached", job.query)
                else:
                    await self._cache.set(job.query, results, engine)

                logger.info(
                    "query=%r engine=%s results=%d served",
                    job.query, engine, len(results),
                )
                return SearchResponse(
                    query=job.query,
                    engine_used=engine,
                    cached=False,
                    execution_time_ms=0,
                    results_count=len(results),
                    results=results,
                )
            except EngineBlockedError as exc:
                await self._engines.report_failure(engine, exc.reason)
                # Job.requested_engine is quarantined now; the next iteration
                # resolves a fresh engine via Round-Robin.
                continue
            except AllEnginesQuarantinedError:
                raise
            finally:
                self._release_engine(engine)
        raise AllEnginesQuarantinedError()

    # -- per-engine concurrency (isolation) ---------------------------------

    async def _reserve_engine(self, requested: str | None) -> str:
        """Pick an engine and hold its per-engine slot for the duration.

        Rotation prefers an engine with a free slot, so a slow or hanging engine
        cannot consume the capacity of the healthy ones.
        """
        cap = max(1, self._settings.search_max_concurrent_per_engine)
        for _ in range(max(1, len(self._engines.engines))):
            engine = await self._engines.resolve_engine(requested)
            if self._inflight.get(engine, 0) < cap:
                self._inflight[engine] = self._inflight.get(engine, 0) + 1
                return engine
            # Every slot is taken: rotate and try the next engine.
        engine = await self._engines.resolve_engine(requested)
        self._inflight[engine] = self._inflight.get(engine, 0) + 1
        logger.debug("engine=%s is at its concurrency cap; overloading it", engine)
        return engine

    def _release_engine(self, engine: str) -> None:
        remaining = self._inflight.get(engine, 0) - 1
        if remaining > 0:
            self._inflight[engine] = remaining
        else:
            self._inflight.pop(engine, None)

    # -- supervision --------------------------------------------------------

    async def _monitor_loop(self) -> None:
        """Sample the queue and the workers until cancelled."""
        interval = max(0.5, float(self._settings.search_monitor_interval_seconds))
        while True:
            await asyncio.sleep(interval)
            try:
                self._monitor_tick()
            except Exception:  # pragma: no cover - the supervisor must not die
                logger.warning("search supervisor tick failed", exc_info=True)

    def _monitor_tick(self) -> None:
        """One supervision pass: replace the dead, notice the stuck, judge it."""
        self._supervise()
        self._refresh_worker_ages()
        depth = self._queue.qsize()
        self._breaker.observe(
            depth=depth,
            stuck=self._queue_stuck(depth),
            dispatcher_stalled=self._dispatcher_stalled(depth),
        )

    def _supervise(self) -> None:
        """Replace a worker that died, failing whatever it left queued."""
        if self._stopping:
            return
        for index, worker in list(self._workers.items()):
            if not worker.task.done():
                continue
            error: BaseException | None = None
            with suppress(asyncio.CancelledError):
                error = worker.task.exception()
            worker.state = WORKER_DEAD
            logger.error(
                "search worker %d died (%r); replacing it and failing its queue",
                index, error,
            )
            self._drain_queue(
                SearchUnavailableError(
                    "worker_died",
                    "a search worker crashed; its queued work was failed",
                    queue_depth=self._queue.qsize(),
                    worker_state=WORKER_DEAD,
                    retry_after=self._breaker.retry_after(),
                )
            )
            self._spawn_worker(index)
            self.metrics.worker_restarts += 1
            if self._registry is not None:
                self._registry.increment("muninn_search_worker_restarts_total")
            self._breaker.note_failure("worker_died")

    def _drain_queue(self, error: BaseException) -> int:
        """Fail every queued job rather than leave the entries to rot."""
        failed = 0
        while True:
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return failed
            job.abandoned = True
            try:
                self._settle(job, error=error)
            finally:
                self._queue.task_done()
            failed += 1

    def _refresh_worker_ages(self) -> None:
        """Mark a worker that is busy well past its deadline as stuck."""
        now = time.monotonic()
        limit = max(1.0, float(self._settings.search_job_deadline_seconds)) + max(
            0.0, float(self._settings.search_worker_stall_grace_seconds)
        )
        for worker in self._workers.values():
            if worker.job_started_at is not None and now - worker.job_started_at > limit:
                worker.state = WORKER_STUCK

    def _queue_stuck(self, depth: int) -> bool:
        """True when the queue is not draining.

        Queue depth alone is not enough: the throttle means a healthy queue can
        sit at the same depth for a while while jobs complete, and tripping on
        that would refuse work the service could serve. The signal is "the queue
        is not empty and nothing has completed in a long time", which is also how
        a dead worker with a populated queue looks from outside.
        """
        if self.worker_state() in {WORKER_DEAD, WORKER_STUCK}:
            return True
        if depth <= 0:
            return False
        stall = max(1.0, float(self._settings.search_breaker_stall_seconds))
        return (time.monotonic() - self._last_progress_at) >= stall

    def _dispatcher_stalled(self, depth: int) -> bool:
        """A non-empty queue that no worker has picked up: a broken dispatcher.

        This is the signature that matters and the one that cannot be argued
        with: while jobs sit in the queue, every worker must be holding one
        (running it or waiting for its throttle slot). All of them idle means the
        workers and the queue have lost contact - a lost wakeup, a crashed worker
        the supervisor has not replaced yet, or a bug in this file.

        It only became checkable once the throttle wait stopped reporting itself
        as idle: before that, a perfectly healthy throttle-bound queue showed up
        here on most samples, and the check would have been noise.
        """
        if depth <= 0:
            return False
        return all(w.state == WORKER_IDLE for w in self._workers.values())

    # -- metrics ------------------------------------------------------------

    def _observe_queue_wait(self, job: SearchJob, now: float) -> None:
        """How long the job sat in the queue before a worker picked it up.

        This is the throttle plus any backlog, not the engine, so it is the
        number that answers "why is search slow" - and it was declared but never
        recorded until the queue could stop draining unnoticed.
        """
        if self._registry is None or job.enqueued_at is None:
            return
        self._registry.observe(
            "muninn_search_queue_wait_seconds", max(0.0, now - job.enqueued_at)
        )

    def _observe_job(self, duration: float, outcome: str) -> None:
        if self._registry is None:
            return
        self._registry.observe("muninn_search_job_seconds", duration, {"outcome": outcome})
        self._registry.increment("muninn_search_jobs_total", {"outcome": outcome})
