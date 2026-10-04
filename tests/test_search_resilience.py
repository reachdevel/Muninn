"""Search-path resilience: the acceptance tests for the wedge hardening.

Muninn's search path once wedged completely while ``/health/*`` and ``/scrape``
stayed fast. The queue stopped draining, every query still waited the caller's
full 120 s before returning 504, and ``circuit_breaker_trips`` and
``queue_rejections`` stayed at zero - so nothing reported a fault.

These tests cover the six acceptance criteria:

1. wedge recovery: a hung engine is killed at the job deadline, the worker is
   released, and the hung engine takes exactly one failure rather than
   blocking the pool;
2. breaker fast-fail and self-recovery: new work is refused in milliseconds
   while the workers are stuck, and the half-open probe closes the breaker with
   no restart;
3. queue drain: a burst larger than the pool can serve reaches zero, and the
   depth is observably falling on the way;
4. quarantine: a failing engine does not reduce healthy-engine capacity, and
   cooldowns stay bounded and get re-probed;
5. a pinned quarantined engine is refused immediately (in ``test_api.py``,
   which drives the same thing over HTTP);
6. the status-code contract is unchanged, including ``cached: true`` on replays.

No network and no browser: every fetch goes through a driver the test controls.
"""

from __future__ import annotations

import asyncio
import time
from time import perf_counter

import pytest
from fastapi.testclient import TestClient

from app.cache import SearchCache
from app.config import SUPPORTED_ENGINES, Settings
from app.engine_manager import EngineManager
from app.search_service import (
    EngineQuarantinedError,
    SearchJob,
    SearchJobFailedError,
    SearchQueueFullError,
    SearchService,
    SearchUnavailableError,
)
from drivers.parsers import BingParser
from tests.conftest import make_test_settings
from tests.html_fixtures import BLOCK_PAGES, ENGINE_RESULTS_HTML


class ControllableDriver:
    """A search driver the test steers: per-engine latency, hangs and blocks.

    ``hang`` is the black-holed engine from the incident report: a fetch that
    never returns and can only be ended by cancelling it.
    """

    def __init__(self) -> None:
        self._started = False
        self.latency = 0.0
        self.hang: set[str] = set()
        self.block: set[str] = set()
        self.calls: list[str] = []
        self.starts: list[float] = []

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    @property
    def is_started(self) -> bool:
        return self._started

    async def fetch_html(self, url: str, engine: str = "") -> tuple[str, int]:
        self.calls.append(engine)
        self.starts.append(time.monotonic())
        if engine in self.hang:
            # A black hole: nothing comes back, and only cancellation ends it.
            await asyncio.sleep(3600)
        if self.latency:
            await asyncio.sleep(self.latency)
        if engine in self.block:
            return BLOCK_PAGES[engine], 200
        return ENGINE_RESULTS_HTML[engine], 200


async def make_service(
    driver: ControllableDriver | None = None, **overrides
) -> tuple[SearchService, EngineManager, Settings]:
    """A live SearchService on a real in-memory cache, no app, no browser."""
    driver = driver or ControllableDriver()
    settings = make_test_settings(**overrides)
    cache = SearchCache(settings.cache_db_path, settings.cache_ttl_seconds)
    await cache.connect()
    engines = EngineManager(settings)
    service = SearchService(settings, driver, cache, engines)
    await service.start()
    return service, engines, settings


async def stop_service(service: SearchService) -> None:
    await service.stop()
    cache: SearchCache = service._cache  # noqa: SLF001 - owned by this test
    await cache.close()


# ------------------------------------------------- 1. wedge recovery (P0 deadline)


async def test_a_hung_engine_is_killed_at_the_deadline_and_the_worker_is_released() -> None:
    """Acceptance 1: the P0 deadline, not the caller's patience, ends a hung job.

    The engine in flight is charged exactly one failure, and the pool keeps its
    other three engines - one bad engine must not become no engines.
    """
    driver = ControllableDriver()
    driver.hang = {"google"}
    service, engines, settings = await make_service(
        driver,
        search_job_deadline_seconds=0.5,
        search_worker_count=1,
        quarantine_first_seconds=1,
        quarantine_escalated_seconds=1,
    )
    try:
        # Pinned, so rotation cannot route around the hang: with the default
        # strategy a hung engine in the first group is simply skipped and the job
        # succeeds elsewhere, which is the resilience working - but then there is
        # no deadline to observe.
        started = perf_counter()
        with pytest.raises(SearchJobFailedError):
            await service.submit(query="hung", max_results=10,
                                 requested_engine="google", force_refresh=False)
        elapsed = perf_counter() - started

        # Killed at the deadline, not at REQUEST_TIMEOUT_SECONDS.
        assert elapsed < float(settings.request_timeout_seconds) / 2
        assert service.metrics.jobs_deadline_killed == 1
        # The hung engine took the failure...
        assert engines._states["google"].fail_count == 1  # noqa: SLF001
        # ...and the pool is still a pool.
        assert set(engines.active_engines()) == set(SUPPORTED_ENGINES)

        # The worker was released, so the next search is served normally.
        driver.hang = set()
        response = await service.submit(query="after the hang", max_results=10,
                                        requested_engine=None, force_refresh=False)
        assert response.cached is False
        assert response.results_count >= 1
        assert service.worker_status()["state"] == "idle"
    finally:
        await stop_service(service)


async def test_a_caller_that_gives_up_does_not_kill_the_worker() -> None:
    """The regression that caused the incident, in full.

    ``submit`` waited on the job future with ``wait_for``, which cancels the
    future on timeout. The worker later resolved that cancelled future, raised
    ``InvalidStateError`` inside its own error handler, and died - taking the
    only worker with it. The queue then never drained again, which is exactly
    the observed wedge: every request timed out at 120 s while the health
    endpoints kept answering in 80 ms.
    """
    driver = ControllableDriver()
    driver.latency = 1.2
    service, _engines, settings = await make_service(
        driver, request_timeout_seconds=1, search_job_deadline_seconds=5
    )
    try:
        with pytest.raises(TimeoutError):
            await service.submit(query="abandoned", max_results=10,
                                 requested_engine=None, force_refresh=False)

        # Let the abandoned job finish, as it would have in production.
        await asyncio.sleep(1.35)
        assert service.metrics.jobs_abandoned == 1

        # Every worker is still alive.
        for worker in service._workers.values():  # noqa: SLF001
            assert not worker.task.done(), "a caller timeout killed a worker"
        assert service.metrics.worker_restarts == 0

        # And the queue still drains.
        driver.latency = 0.0
        response = await service.submit(query="after the abandonment", max_results=10,
                                        requested_engine=None, force_refresh=False)
        assert response.results_count >= 1
        assert settings.request_timeout_seconds == 1
    finally:
        await stop_service(service)


async def test_a_disconnected_client_releases_the_worker_early() -> None:
    """Best effort: an abandoned request should stop consuming a worker.

    The deadline remains the guarantee; this only stops the waste sooner.
    """
    driver = ControllableDriver()
    driver.latency = 5.0
    service, _engines, _settings = await make_service(
        driver, request_timeout_seconds=30, search_job_deadline_seconds=20
    )
    try:
        async def disconnected() -> bool:
            return True

        with pytest.raises(Exception) as caught:
            await service.submit(query="client hung up", max_results=10,
                                 requested_engine=None, force_refresh=False,
                                 disconnect_check=disconnected)
        assert "disconnect" in str(caught.value).lower()

        # The in-flight fetch was cancelled rather than left to run for 5s.
        await asyncio.sleep(0.1)
        assert service.worker_status()["state"] == "idle"
        assert service.metrics.jobs_abandoned == 1
    finally:
        await stop_service(service)


async def test_a_worker_pool_does_not_defeat_the_throttle() -> None:
    """Politeness must survive the worker pool.

    With one worker the outbound gap was free. With four, if the throttle were
    per-worker, all four would find the gap elapsed at the same instant and fire
    together - quadrupling the instantaneous rate the throttle exists to cap.
    """
    driver = ControllableDriver()
    service, _engines, _settings = await make_service(
        driver,
        search_worker_count=4,
        throttle_min_delay=0.1,
        throttle_max_delay=0.1,
        search_max_concurrent_per_engine=4,
        search_job_deadline_seconds=5,
    )
    try:
        responses = await asyncio.gather(
            *(
                service.submit(query=f"throttled {i}", max_results=10,
                               requested_engine=None, force_refresh=False)
                for i in range(4)
            )
        )
        assert all(r.results_count >= 1 for r in responses)
        assert len(driver.starts) == 4
        gaps = [
            later - earlier
            for earlier, later in zip(driver.starts, driver.starts[1:], strict=False)
        ]
        assert min(gaps) >= 0.05, f"outbound requests bunched up: gaps={gaps}"
    finally:
        await stop_service(service)


# --------------------------------------- 2. breaker fast-fail + self-recovery (P0)


async def test_the_breaker_refuses_in_milliseconds_and_recovers_on_its_probe() -> None:
    """Acceptance 2: fast 503, then self-recovery with no restart."""
    driver = ControllableDriver()
    driver.hang = {"google"}
    service, _engines, _settings = await make_service(
        driver,
        search_job_deadline_seconds=0.3,
        search_breaker_failure_threshold=1,
        search_breaker_open_seconds=0.3,
        search_breaker_max_open_seconds=0.3,
        search_monitor_interval_seconds=0.05,
        quarantine_first_seconds=1,
        quarantine_escalated_seconds=1,
    )
    try:
        # One deadline kill trips the breaker.
        with pytest.raises(SearchJobFailedError):
            await service.submit(query="trip it", max_results=10,
                                 requested_engine="google", force_refresh=False)
        assert service.breaker.state == "open"

        # While it is refusing, new work is turned away immediately.
        started = perf_counter()
        with pytest.raises(SearchUnavailableError) as caught:
            await service.submit(query="refused", max_results=10,
                                 requested_engine=None, force_refresh=False)
        refused_in = perf_counter() - started
        assert refused_in < 0.25, f"refusal took {refused_in:.3f}s; it must be immediate"
        assert caught.value.reason == "breaker_open"
        assert caught.value.queue_depth == 0
        assert caught.value.worker_state in {"idle", "busy", "stuck"}

        # Still refusing while the backoff has not elapsed.
        with pytest.raises(SearchUnavailableError):
            await service.submit(query="refused again", max_results=10,
                                 requested_engine=None, force_refresh=False)

        # After it, exactly one probe is admitted. It succeeds, so the breaker
        # closes and traffic resumes - no operator, no restart.
        await asyncio.sleep(0.35)
        driver.hang = set()
        probe = await service.submit(query="half-open probe", max_results=10,
                                     requested_engine=None, force_refresh=False)
        assert probe.results_count >= 1
        assert service.breaker.state == "closed"

        for i in range(3):
            assert (await service.submit(query=f"traffic {i}", max_results=10,
                                         requested_engine=None,
                                         force_refresh=False)).results_count >= 1
        assert service.metrics.worker_restarts == 0
    finally:
        await stop_service(service)


async def test_a_failed_probe_reopens_the_breaker_with_a_longer_backoff() -> None:
    """The other half of self-recovery: a probe that fails must not close it."""
    driver = ControllableDriver()
    driver.hang = {"google"}
    service, _engines, _settings = await make_service(
        driver,
        search_job_deadline_seconds=0.3,
        search_breaker_failure_threshold=1,
        search_breaker_open_seconds=0.2,
        search_breaker_max_open_seconds=10.0,
        search_monitor_interval_seconds=0.05,
        quarantine_first_seconds=1,
        quarantine_escalated_seconds=1,
    )
    try:
        with pytest.raises(SearchJobFailedError):
            await service.submit(query="trip", max_results=10,
                                 requested_engine="google", force_refresh=False)
        first_backoff = float(service.breaker.snapshot()["backoff_seconds"])

        # The probe hangs too, so it hits the deadline instead of recovering.
        await asyncio.sleep(0.25)
        with pytest.raises(SearchJobFailedError):
            await service.submit(query="failing probe", max_results=10,
                                 requested_engine="google", force_refresh=False)
        assert service.breaker.state == "open"
        assert float(service.breaker.snapshot()["backoff_seconds"]) > first_backoff
    finally:
        await stop_service(service)


async def test_a_full_queue_is_refused_rather_than_waited_on() -> None:
    """A queue that cannot drain must not accumulate promises."""
    driver = ControllableDriver()
    # Rotation routes around a hung engine, so the queue can only fill when every
    # engine it could reach is busy. One worker, everything hung.
    driver.hang = set(SUPPORTED_ENGINES)
    service, _engines, _settings = await make_service(
        driver,
        max_search_queue=2,
        search_job_deadline_seconds=1,
        search_queue_wait_deadline_seconds=30,
        search_worker_count=1,
    )
    try:
        pending = [
            asyncio.ensure_future(
                service.submit(query=f"q{i}", max_results=10,
                               requested_engine=None, force_refresh=False)
            )
            for i in range(4)
        ]
        # Let them take the worker and fill both queue slots first.
        await asyncio.sleep(0.1)
        assert service.queue_depth == 2

        with pytest.raises(SearchQueueFullError) as caught:
            await service.submit(query="overflow", max_results=10,
                                 requested_engine=None, force_refresh=False)
        assert caught.value.reason == "queue_full"
        assert caught.value.retry_after >= 5
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await stop_service(service)


class WorkerCrashed(BaseException):
    """Not an ``Exception``: the worker loop swallows those on purpose.

    Used to simulate a worker that dies the way a real one does - abruptly, and
    for a reason the loop's own error handling cannot see.
    """


async def test_a_crashed_worker_is_replaced_and_the_pool_keeps_serving() -> None:
    """P1: a dead worker is replaced instead of quietly reducing the pool.

    A crashed worker and a hung one look identical from outside - /health
    answers, /search does not - so both have to be recoverable without an
    operator.
    """
    service, _engines, _settings = await make_service(
        ControllableDriver(),
        search_worker_count=1,
        search_monitor_interval_seconds=0.05,
    )
    original = service._run_job  # noqa: SLF001

    async def crashing(worker, job):  # type: ignore[no-untyped-def]
        # Only the first job takes the worker down with it.
        service._run_job = original  # noqa: SLF001
        job.future.set_exception(SearchJobFailedError("the worker died mid-job"))
        raise WorkerCrashed("simulated worker crash")

    try:
        service._run_job = crashing  # noqa: SLF001
        with pytest.raises(SearchJobFailedError):
            await service.submit(query="kills the worker", max_results=10,
                                 requested_engine=None, force_refresh=False)

        for _ in range(40):
            if service.metrics.worker_restarts:  # noqa: SLF001
                break
            await asyncio.sleep(0.05)
        assert service.metrics.worker_restarts == 1
        assert not service._workers[0].task.done()  # noqa: SLF001
        assert service.worker_state() != "dead"

        # The pool serves again without a restart.
        assert (
            await service.submit(query="after the restart", max_results=10,
                                 requested_engine=None, force_refresh=False)
        ).results_count >= 1
    finally:
        await stop_service(service)


async def test_orphaned_queue_entries_are_failed_not_left_to_rot() -> None:
    """Whatever a dead worker left behind must be resolved, not abandoned.

    A future nobody settles is a caller waiting out its own timeout, which is
    how a queue stops draining without anything reporting a fault.
    """
    service, _engines, _settings = await make_service(ControllableDriver())
    try:
        jobs = [
            SearchJob(query=f"orphan {i}", max_results=10,
                      requested_engine=None, force_refresh=False)
            for i in range(3)
        ]
        for job in jobs:
            service._queue.put_nowait(job)
        assert service.queue_depth == 3

        assert service._drain_queue(  # noqa: SLF001
            SearchUnavailableError("worker_died", "gone")
        ) == 3
        assert service.queue_depth == 0
        for job in jobs:
            assert job.future.done()
            assert job.abandoned
            with pytest.raises(SearchUnavailableError):
                await job.future
    finally:
        await stop_service(service)


# ---------------------------------------------------------- 3. the queue drains


async def test_a_burst_larger_than_the_pool_drains_to_zero() -> None:
    """Acceptance 3: the queue goes back to zero, and the fall is observable."""
    driver = ControllableDriver()
    driver.latency = 0.02
    service, _engines, _settings = await make_service(
        driver, search_worker_count=1, search_job_deadline_seconds=5
    )
    try:
        depths: list[int] = []

        async def sample() -> None:
            while True:
                depths.append(service.queue_depth)
                await asyncio.sleep(0.01)

        sampler = asyncio.ensure_future(sample())
        burst = 12
        responses = await asyncio.gather(
            *(
                service.submit(query=f"burst {i}", max_results=10,
                               requested_engine=None, force_refresh=False)
                for i in range(burst)
            )
        )
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)

        assert len(responses) == burst
        assert all(r.results_count >= 1 for r in responses)
        assert service.queue_depth == 0
        # The backlog built up and then came back down: without this the queue
        # could stop draining and every assertion above would still have passed
        # one at a time.
        assert max(depths) > 0, "the burst never queued; the test proved nothing"
        assert depths[-1] == 0
    finally:
        await stop_service(service)


# ------------------------------------------------------------- 4. quarantine


async def test_one_failing_engine_does_not_reduce_healthy_capacity() -> None:
    """Acceptance 4: google blocks, the other three keep the service serving."""
    driver = ControllableDriver()
    driver.block = {"google"}
    service, engines, settings = await make_service(
        driver,
        quarantine_first_seconds=5,
        quarantine_escalated_seconds=20,
        search_job_deadline_seconds=5,
    )
    try:
        served_by: list[str] = []
        for i in range(12):
            response = await service.submit(query=f"mixed {i}", max_results=10,
                                            requested_engine=None, force_refresh=False)
            served_by.append(response.engine_used)

        assert served_by, "nothing was served at all"
        assert "google" not in served_by, "a quarantined engine was still used"
        # google is out; the healthy engines are all still in rotation.
        assert "google" not in engines.active_engines()
        assert set(engines.active_engines()) == set(SUPPORTED_ENGINES) - {"google"}

        # And every cooldown in the pool is bounded by the configured cap.
        for state in engines._states.values():  # noqa: SLF001
            assert state.remaining_cooldown() <= settings.quarantine_escalated_seconds
    finally:
        await stop_service(service)


def test_cooldowns_grow_exponentially_and_are_capped() -> None:
    """Bounded escalation: longer each time, never past the ceiling."""
    settings = make_test_settings(
        quarantine_first_seconds=300, quarantine_escalated_seconds=1_800
    )
    manager = EngineManager(settings)
    durations = [
        manager._cooldown_seconds(fails, "block")  # noqa: SLF001
        for fails in range(1, 8)
    ]
    assert durations[:4] == [300, 600, 1_200, 1_800]
    # The ceiling is a ceiling, not an asymptote.
    assert durations[4:] == [1_800, 1_800, 1_800]


def test_failure_classes_are_not_all_worth_the_same() -> None:
    """A DNS blip must not cost the same cooldown as a CAPTCHA."""
    settings = make_test_settings(
        quarantine_first_seconds=300, quarantine_escalated_seconds=1_800
    )
    manager = EngineManager(settings)
    block = manager._cooldown_seconds(1, "block")  # noqa: SLF001
    assert manager._cooldown_seconds(1, "timeout") < block  # noqa: SLF001
    assert manager._cooldown_seconds(1, "network") < block  # noqa: SLF001
    assert manager._cooldown_seconds(1, "parse") < block  # noqa: SLF001


def test_failure_reasons_are_classified() -> None:
    from app.engine_manager import classify_failure

    assert classify_failure("429") == "block"
    assert classify_failure("captcha") == "block"
    assert classify_failure("empty-response") == "block"
    assert classify_failure("job-deadline") == "timeout"
    assert classify_failure("connection reset") == "network"
    assert classify_failure("no-results") == "parse"


# ------------------------------------------------ the breaker state machine itself


class FakeClock:
    """A clock the test owns, so half-open timing needs no sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _breaker(clock: FakeClock, **overrides):
    from app.search_breaker import SearchBreaker

    settings = make_test_settings(
        search_breaker_failure_threshold=2,
        search_breaker_open_seconds=10,
        search_breaker_max_open_seconds=40,
        **overrides,
    )
    return SearchBreaker(settings, clock=clock)


def test_a_closed_breaker_admits_everything() -> None:
    breaker = _breaker(FakeClock())
    assert breaker.admit().admitted is True
    assert breaker.admit(queue_depth=5).admitted is True
    assert breaker.state == "closed"


def test_a_full_queue_is_refused_without_opening_the_breaker() -> None:
    """Queue-full is an honest refusal, not a service fault: refuse, stay closed."""
    breaker = _breaker(FakeClock(), max_search_queue=10)
    decision = breaker.admit(queue_depth=10)
    assert decision.admitted is False
    assert decision.reason == "queue_full"
    assert breaker.state == "closed"


def test_the_breaker_opens_after_the_threshold_then_probes_exactly_once() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)

    breaker.note_failure("job_deadline")
    assert breaker.admit().admitted is True, "one failure is not a pattern yet"

    breaker.note_failure("job_deadline")
    assert breaker.state == "open"
    refused = breaker.admit()
    assert refused.admitted is False
    assert refused.reason == "breaker_open"

    # After the backoff: one probe, and only one.
    clock.advance(10)
    probe = breaker.admit()
    assert probe.admitted is True
    assert probe.probe is True
    assert probe.state == "half_open"
    second = breaker.admit()
    assert second.admitted is False
    assert second.reason == "probe_in_flight"

    # The probe completed: closed, and the backoff resets.
    breaker.note_job_completed()
    assert breaker.state == "closed"
    assert breaker.admit().admitted is True
    assert float(breaker.snapshot()["backoff_seconds"]) == 10.0


def test_a_failed_probe_re_opens_with_a_longer_backoff() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    breaker.trip("not_draining")
    assert float(breaker.snapshot()["backoff_seconds"]) == 20.0

    clock.advance(10)
    assert breaker.admit().probe is True
    breaker.note_failure("job_deadline")

    assert breaker.state == "open"
    # 10s elapsed since the last opening, so the new deadline is 10 + 20.
    clock.advance(10)
    assert breaker.admit().admitted is False, "the probe failure must extend it"
    assert float(breaker.snapshot()["backoff_seconds"]) == 40.0

    # And the ceiling holds, however many rounds go by.
    breaker.trip("not_draining")
    assert float(breaker.snapshot()["backoff_seconds"]) == 40.0


def test_one_healthy_job_after_failures_closes_the_breaker() -> None:
    breaker = _breaker(FakeClock())
    breaker.note_failure("job_deadline")
    breaker.note_failure("job_deadline")
    assert breaker.state == "open"
    breaker.note_job_completed()
    assert breaker.state == "closed"
    assert int(breaker.snapshot()["consecutive_failures"]) == 0


def test_a_failed_engine_is_re_probed_rather_than_left_out_for_hours() -> None:
    """The old policy could take the pool from four engines to one for ~12h."""
    default = Settings()
    assert default.quarantine_escalated_seconds <= 1_800, (
        "the escalation ceiling is what takes the pool down to one engine"
    )
    assert default.quarantine_first_seconds < default.quarantine_escalated_seconds


def test_the_metrics_explain_the_next_wedge() -> None:
    """The next incident has to be diagnosable from /metrics alone.

    Before this change the only numbers were `searches_served`, the cache
    counters and two breaker counters that both read zero while the queue never
    drained.
    """
    driver = ControllableDriver()
    driver.hang = {"google"}
    with TestClient(
        _app(driver, search_job_deadline_seconds=0.25, search_worker_count=1)
    ) as client:
        # Pinned: unpinned, the grouped strategy routes straight past the hang.
        resp = client.get("/search", params={"q": "hangs", "engine": "google"})
        assert resp.status_code == 504
        text = client.get("/metrics").text

    assert 'muninn_search_deadline_kills_total{engine="google"} 1' in text
    assert 'muninn_search_jobs_total{outcome="deadline"} 1' in text
    # Per-job duration, so a tail of deadline-sized jobs is visible.
    assert 'muninn_search_job_seconds_bucket{outcome="deadline",le="+Inf"} 1' in text
    # Worker and breaker state, both back to idle/closed: it recovered.
    assert "muninn_search_worker_state 0" in text
    assert "muninn_search_breaker_state 0" in text
    assert "muninn_search_workers 1" in text
    assert "muninn_search_queue_stuck 0" in text


# ---------------------------------------------------- 6. the HTTP status contract


def _app(driver: ControllableDriver, **overrides):
    from app.main import create_app

    return create_app(
        settings=make_test_settings(**overrides), driver_factory=lambda s: driver
    )


def test_status_codes_keep_their_meaning() -> None:
    """Acceptance 6: the client's status-code table must not drift."""
    driver = ControllableDriver()
    with TestClient(_app(driver)) as client:
        # 200: success, and cached results are free.
        ok = client.get("/search", params={"q": "contract"})
        assert ok.status_code == 200
        assert ok.json()["cached"] is False
        calls = len(driver.calls)
        replay = client.get("/search", params={"q": "contract"})
        assert replay.status_code == 200
        assert replay.json()["cached"] is True
        assert len(driver.calls) == calls, "a cache hit must not touch an engine"

        # 422: the query is invalid; the client skips it and carries on.
        assert client.get("/search").status_code == 422
        assert client.get("/search", params={"q": ""}).status_code == 422
        assert client.get("/search", params={"q": "x", "engine": "altavista"}).status_code == 422

        # 503: no engine left to serve it.
        driver.block = set(SUPPORTED_ENGINES)
        assert client.get("/search", params={"q": "blocked"}).status_code == 503


def test_429_is_reserved_for_the_rate_limiter() -> None:
    """429 must keep its meaning, and must keep carrying Retry-After."""
    driver = ControllableDriver()
    with TestClient(_app(driver, search_rate_limit_per_minute=1)) as client:
        assert client.get("/search", params={"q": "first"}).status_code == 200
        limited = client.get("/search", params={"q": "second"})
        assert limited.status_code == 429
        assert limited.json()["error"] == "rate_limited"
        assert int(limited.headers["Retry-After"]) >= 1


def test_504_is_a_job_that_ran_and_failed_not_a_refusal() -> None:
    """The split the contract asks for: 503 = cannot serve now, 504 = it failed.

    A hung engine is a genuine per-job failure, so it is a 504 - and crucially
    not a 503, which the client reads as "the backends are unhealthy, defer".
    """
    driver = ControllableDriver()
    driver.hang = {"google"}
    with TestClient(
        _app(driver, search_job_deadline_seconds=0.25, search_worker_count=1)
    ) as client:
        started = perf_counter()
        # Pinned: unpinned, the grouped strategy routes straight past the hang.
        resp = client.get("/search", params={"q": "will not answer", "engine": "google"})
        assert resp.status_code == 504
        assert perf_counter() - started < 5, "the job deadline must bound this"
        assert "deadline" in resp.json()["detail"]


def test_a_refusal_carries_a_machine_readable_reason() -> None:
    """The 503 body has to be actionable without reading the logs."""
    import threading

    driver = ControllableDriver()
    driver.hang = set(SUPPORTED_ENGINES)
    with TestClient(
        _app(
            driver,
            max_search_queue=1,
            search_job_deadline_seconds=2,
            search_queue_wait_deadline_seconds=30,
            search_worker_count=1,
        )
    ) as client:
        def fire(query: str) -> int:
            return client.get("/search", params={"q": query}).status_code

        def wait_for(predicate, timeout: float = 5.0) -> bool:
            deadline = perf_counter() + timeout
            while perf_counter() < deadline:
                if predicate():
                    return True
                time.sleep(0.02)
            return False

        # Occupy the single worker, then the single queue slot, then ask again.
        holder = threading.Thread(target=fire, args=("occupies the worker",))
        holder.start()
        busy = wait_for(
            lambda: client.get("/health/live").json()["worker"]["state"] == "busy"
        )
        assert busy, "the worker never picked the job up"

        filler = threading.Thread(target=fire, args=("fills the queue",))
        filler.start()
        filled = wait_for(lambda: client.get("/status").json()["queue_depth"] == 1)
        assert filled, "the queue never took the filler job"

        started = perf_counter()
        refused = client.get("/search", params={"q": "one too many"})
        refused_in = perf_counter() - started
        holder.join(timeout=15)
        filler.join(timeout=15)

        assert refused.status_code == 503
        assert refused_in < 1.0, f"the refusal took {refused_in:.3f}s"
        body = refused.json()
        assert body["reason"] == "queue_full"
        assert body["queue_depth"] >= 1
        assert body["worker_state"] in {"idle", "busy", "stuck", "dead"}
        assert int(refused.headers["Retry-After"]) >= 1


# ---------------------------- the four defects found in post-P0 testing
#
# These are the shapes that survived the first pass. They share one cause: the
# throttle wait sat between the dequeue and the job, outside everything the
# service measured, so a healthy throttle-bound queue looked like a dead
# dispatcher - and a queue nobody was measuring cannot be bounded or refused.


async def test_a_non_empty_queue_always_has_a_worker_holding_a_job() -> None:
    """Defect 1: workers reporting idle while the queue is full.

    Throttle deliberately longer than the fetch, which is the real ratio: 15-30s
    of politeness against a 5-18s engine. Under that ratio almost every sample
    found both workers "idle" with jobs queued, which is indistinguishable from a
    lost wakeup - and there was no lost wakeup, only a throttle sleep that did not
    report itself.
    """
    driver = ControllableDriver()
    driver.latency = 0.25
    service, _engines, _settings = await make_service(
        driver,
        search_worker_count=2,
        search_max_concurrent_per_engine=1,
        # Throttle several times the fetch, which is the ratio that matters: the
        # first fetch never waits, every one after it waits most of its life.
        throttle_min_delay=0.6,
        throttle_max_delay=0.9,
        search_job_deadline_seconds=30,
        search_queue_wait_deadline_seconds=30,
        request_timeout_seconds=60,
    )
    try:
        violations: list[tuple[int, list[str]]] = []
        sampling = True

        async def sample() -> None:
            while sampling:
                depth = service.queue_depth
                states = [w.state for w in service._workers.values()]
                if depth > 0 and all(s == "idle" for s in states):
                    violations.append((depth, states))
                await asyncio.sleep(0.02)

        sampler = asyncio.ensure_future(sample())
        await asyncio.gather(
            *(
                service.submit(query=f"burst {i}", max_results=10,
                               requested_engine=None, force_refresh=False)
                for i in range(8)
            )
        )
        sampling = False
        await asyncio.gather(sampler, return_exceptions=True)

        assert not violations, (
            f"{len(violations)} samples had queued jobs and no worker holding any: "
            f"the dispatcher (or the reporting) is broken again"
        )
        assert service.queue_depth == 0
    finally:
        await stop_service(service)


async def test_a_throttled_worker_says_so() -> None:
    """The state that makes the invariant above checkable."""
    driver = ControllableDriver()
    driver.latency = 0.1
    service, _engines, _settings = await make_service(
        driver,
        search_worker_count=1,
        throttle_min_delay=1.0,
        throttle_max_delay=1.0,
    )
    try:
        # The first fetch is never throttled, so warm the clock up first.
        await service.submit(query="warm up", max_results=10,
                             requested_engine=None, force_refresh=False)

        seen: list[str] = []
        pending = asyncio.ensure_future(
            service.submit(query="throttle me", max_results=10,
                           requested_engine=None, force_refresh=False)
        )
        for _ in range(40):
            seen.append(service.worker_state())
            await asyncio.sleep(0.05)
        await pending
        assert "throttled" in seen, f"never reported the throttle wait: {set(seen)}"
        assert "busy" in seen
        assert service.worker_status()["state"] == "idle"
    finally:
        await stop_service(service)


async def test_a_job_that_cannot_start_fails_fast_instead_of_hanging() -> None:
    """Defect 2: the deadline only covered execution, so a job that never got
    dispatched was never killed - the caller just waited."""
    driver = ControllableDriver()
    service, _engines, settings = await make_service(
        driver,
        search_worker_count=1,
        throttle_min_delay=30.0,
        throttle_max_delay=30.0,
        search_queue_wait_deadline_seconds=1.0,
        search_job_deadline_seconds=30.0,
        request_timeout_seconds=30,
    )
    try:
        # Occupy the single worker, then queue behind it.
        held = asyncio.ensure_future(
            service.submit(query="held", max_results=10,
                           requested_engine=None, force_refresh=False)
        )
        await asyncio.sleep(0.05)

        started = perf_counter()
        with pytest.raises(SearchUnavailableError) as caught:
            await service.submit(query="will never start", max_results=10,
                                 requested_engine=None, force_refresh=False)
        waited = perf_counter() - started

        assert caught.value.reason == "queue_wait_timeout"
        # Fast, and long before the caller's own timeout.
        assert waited < 2.0, f"took {waited:.2f}s to refuse"
        assert waited < float(settings.request_timeout_seconds)
        assert service.metrics.jobs_queue_wait_timed_out >= 1
        # A refusal, not a failure: nothing ran, so the caller should defer.
        assert not isinstance(caught.value, SearchJobFailedError)

        held.cancel()
        await asyncio.gather(held, return_exceptions=True)
    finally:
        await stop_service(service)


def test_the_breaker_opens_when_nothing_picks_up_the_queue() -> None:
    """Defect 3: an idle pool over a full queue is a provable deadlock."""
    clock = FakeClock()
    breaker = _breaker(clock)
    breaker.observe(depth=4, stuck=False, dispatcher_stalled=True)
    assert breaker.state == "open"
    assert breaker.snapshot()["reason"] == "no_worker_picking_up"
    # ...and it recovers on a probe rather than latching.
    clock.advance(10)
    assert breaker.admit().probe is True
    breaker.note_job_completed()
    assert breaker.state == "closed"


async def test_the_supervisor_notices_a_dispatcher_that_stopped_picking_up() -> None:
    """The wiring, not just the predicate.

    Models a *live* worker pool that has stopped taking jobs - the lost-wakeup
    shape - rather than a dead one, because the supervisor would replace a dead
    worker and there would be nothing to notice. With the supervisor stopped (so
    it cannot quietly fix things) and the workers parked, a full queue is exactly
    the signature the breaker exists for.
    """
    service, _engines, _settings = await make_service(
        ControllableDriver(), search_worker_count=2
    )
    try:
        monitor = service._monitor_task  # noqa: SLF001
        assert monitor is not None
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)

        # Park the workers: alive, idle, and not looking at the queue.
        for worker in service._workers.values():  # noqa: SLF001
            worker.task.cancel()
            await asyncio.gather(worker.task, return_exceptions=True)
            worker.task = asyncio.ensure_future(asyncio.sleep(3600))

        service._queue.put_nowait(  # noqa: SLF001
            SearchJob(query="orphan", max_results=10,
                      requested_engine=None, force_refresh=False)
        )

        assert service._dispatcher_stalled(1) is True  # noqa: SLF001
        service._monitor_tick()  # noqa: SLF001

        assert service.breaker.state == "open"
        assert service.breaker.snapshot()["reason"] == "no_worker_picking_up"
        started = perf_counter()
        with pytest.raises(SearchUnavailableError):
            await service.submit(query="refused", max_results=10,
                                 requested_engine=None, force_refresh=False)
        assert perf_counter() - started < 0.5, "the refusal must be immediate"
    finally:
        await stop_service(service)


def test_the_app_middleware_does_not_swallow_client_disconnects() -> None:
    """Defect 4, structurally.

    ``Request.is_disconnected()`` never returns True when the app wraps requests
    in Starlette's ``BaseHTTPMiddleware``, because that layer re-plumbs the
    server's ``receive`` channel and drops ``http.disconnect``. The symptom was
    ``jobs_abandoned`` stuck at 0 forever: five socket-level hang-ups, zero
    detected. A pure-ASGI middleware keeps ``receive`` intact.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.main import create_app
    from tests.conftest import FakeDriver, make_test_settings

    app = create_app(settings=make_test_settings(), driver_factory=lambda s: FakeDriver())
    layers = list(app.user_middleware)  # type: ignore[attr-defined]
    assert layers, "the request-context middleware is missing entirely"
    for layer in layers:
        assert not issubclass(layer.cls, BaseHTTPMiddleware), (
            f"{layer.cls.__name__} is a BaseHTTPMiddleware: /search cannot see a "
            f"client disconnect through it"
        )


# ------------------------------------------------------ yandex / qwant behaviour


async def test_a_blocked_engine_does_not_stop_the_others_or_poison_the_cache() -> None:
    """One walled engine must not take the pool with it.

    Every engine Muninn adds sits behind some form of anti-bot, so this property
    is the one that decides whether the pool is worth extending at all: the
    blocked engine takes exactly one failure, the job lands on a healthy engine,
    and the block page is never cached as an answer.

    The loop drives until the blocked engine has actually been attempted, rather
    than assuming where round-robin lands once the active list starts shrinking.
    """
    from tests.html_fixtures import BLOCK_PAGES, ENGINE_RESULTS_HTML

    # A primary-group engine, so it really is on the hot path and really is
    # charged. (The grouped strategy is covered in test_engine_selection.py.)
    walled = "brave"

    class PartialBlockDriver(ControllableDriver):
        async def fetch_html(self, url: str, engine: str = "") -> tuple[str, int]:
            self.calls.append(engine)
            self.starts.append(time.monotonic())
            if engine == walled:
                return BLOCK_PAGES[engine], 200
            return ENGINE_RESULTS_HTML[engine], 200

    driver = PartialBlockDriver()
    service, engines, _settings = await make_service(
        driver, quarantine_first_seconds=300, quarantine_escalated_seconds=300
    )
    try:
        served = 0
        for i in range(len(SUPPORTED_ENGINES) * 4):
            response = await service.submit(query=f"pool {i}", max_results=10,
                                            requested_engine=None, force_refresh=False)
            assert response.results_count >= 1, "a blocked engine starved the job"
            assert response.engine_used != walled
            served += 1
            if engines._states[walled].fail_count:
                break
        assert engines._states[walled].fail_count >= 1

        state = engines._states[walled]  # noqa: SLF001
        assert state.fail_count == 1, f"{walled} was never charged"
        assert state.last_failure_class == "block"
        assert state.active is False, "a blocked engine stays in rotation"

        # The rest of the pool is untouched - including the fallback group, which
        # the grouped strategy never reaches while the primary one can serve.
        assert set(engines.active_engines()) == set(SUPPORTED_ENGINES) - {walled}
        assert engines._states["mojeek"].fail_count == 0
        assert engines._states["ecosia"].fail_count == 0
        # ...and every answer came from a healthy engine, none from a block page.
        assert await service._cache.count() == served  # noqa: SLF001
    finally:
        await stop_service(service)


async def test_a_pinned_engine_that_blocks_mid_job_is_refused_not_substituted() -> None:
    """A pin is a question, not a preference.

    The caller named an engine because it wanted that index (a `site:` query, a
    locale). When it blocks, another engine's results answer a different question,
    so the request fails with 503 instead of quietly changing the subject.
    """
    driver = ControllableDriver()
    driver.block = {"ddg"}
    service, _engines, _settings = await make_service(driver)
    try:
        with pytest.raises(EngineQuarantinedError) as caught:
            await service.submit(query="site:example.com", max_results=10,
                                 requested_engine="ddg", force_refresh=False)
        assert caught.value.engine == "ddg"

        # Unpinned, the same block just rotates.
        served = await service.submit(query="unpinned", max_results=10,
                                      requested_engine=None, force_refresh=False)
        assert served.engine_used != "ddg"
        assert served.results_count >= 1
    finally:
        await stop_service(service)


async def test_an_unrendered_page_is_a_block_not_an_empty_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The JavaScript-SERP trap, exercised through the real execution path.

    An engine that renders client-side can hand us a page with no results simply
    because we captured it too early. Caching that would serve a wrong answer for
    a day and report a successful search, so it is charged to the engine instead.

    The opt-in is set on the parked Yandex and Qwant parsers, which are not in the
    rotation, so this drives an in-pool engine with the flag turned on: what is
    under test is the guard in the execution path, not a particular parser.
    """
    from drivers.parsers import GoogleParser
    from tests.html_fixtures import QWANT_JS_SHELL_HTML

    monkeypatch.setattr(GoogleParser, "JS_SHELL_SIGNATURES", ("noscript",))

    class ShellDriver(ControllableDriver):
        async def fetch_html(self, url: str, engine: str = "") -> tuple[str, int]:
            self.calls.append(engine)
            if engine == "google":
                return QWANT_JS_SHELL_HTML, 200
            from tests.html_fixtures import ENGINE_RESULTS_HTML
            return ENGINE_RESULTS_HTML[engine], 200

    driver = ShellDriver()
    service, engines, _settings = await make_service(driver)
    try:
        served = 0
        for i in range(len(SUPPORTED_ENGINES) * 4):
            response = await service.submit(query=f"shell {i}", max_results=10,
                                            requested_engine=None, force_refresh=False)
            assert response.engine_used != "google", "an unrendered page counted as a search"
            served += 1
            if "google" in driver.calls:
                break

        # Charged to the engine as a failure, so it drops out of rotation...
        assert engines._states["google"].fail_count == 1  # noqa: SLF001
        assert "google" not in engines.active_engines()
        # ...and the shell contributed nothing to the cache.
        assert await service._cache.count() == served  # noqa: SLF001
    finally:
        await stop_service(service)


async def test_a_site_query_skips_engines_that_render_those_results_client_side() -> None:
    """A `site:` query must not be answered from a shell that parses to nothing.

    Bing and Yahoo return a 240KB shell with no result markup for `site:`
    queries. Treating that as "no results" would cache a false empty answer for a
    day, on the query type callers most need right - so those engines are skipped
    for that shape, cheaply and without being charged a failure.
    """
    from app.search_service import _uses_site_operator

    assert _uses_site_operator("site:example.com")
    assert BingParser.SERVES_SITE_OPERATOR is False

    driver = ControllableDriver()
    service, engines, _settings = await make_service(driver)
    try:
        response = await service.submit(query="site:example.com", max_results=10,
                                        requested_engine=None, force_refresh=False)
        # DuckDuckGo serves site: queries server-side, so the job lands there.
        assert response.engine_used not in {"bing", "yahoo"}
        assert response.results_count >= 1
        # Skipping is not a failure: nothing was charged to the skipped engines.
        assert engines._states["bing"].fail_count == 0  # noqa: SLF001
        assert engines._states["yahoo"].fail_count == 0  # noqa: SLF001
    finally:
        await stop_service(service)
