"""Load breaker for the search path: refuse immediately, recover on a probe.

The wedge this exists to prevent had a signature worth naming: ``/health/*`` and
``/scrape`` answered in under a quarter of a second while ``/search`` returned
504 after exactly the caller's own read timeout, and the queue stopped draining
without ever getting deep. Nothing reported a fault, so the service kept
accepting work into a queue that could never empty.

So the breaker has two jobs, and the second one is the one people forget:

1. **Refuse fast.** When the queue is full, or the workers are not draining, new
   search work is turned away immediately - no enqueue, no wait, single-digit
   milliseconds - with ``503`` and a machine-readable reason. A caller waiting
   120 seconds for a refusal learns nothing; a caller told "come back in 30
   seconds" can.
2. **Never latch.** After refusing for ``SEARCH_BREAKER_OPEN_SECONDS`` the
   breaker goes half-open and admits *exactly one* probe request. If that job
   completes the breaker closes and traffic resumes; if it fails or hits the
   job deadline the breaker re-opens with a longer backoff. Without the probe
   the breaker would refuse forever and the service would need an operator -
   which is the failure mode it is supposed to remove.

State is in-process and every decision is a plain synchronous function of it, so
:meth:`SearchBreaker.admit` costs microseconds and cannot itself wedge.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.config import Settings
from ops.metrics import Registry

logger = logging.getLogger(__name__)

#: Breaker states, mirrored by the ``muninn_search_breaker_state`` gauge.
CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

GAUGE_VALUES: dict[str, int] = {CLOSED: 0, HALF_OPEN: 1, OPEN: 2}

# Machine-readable refusal reasons, surfaced in the 503 body and as the
# ``reason`` label on muninn_search_breaker_rejections_total. The set is closed
# on purpose: the value is a metric label and an operator-facing contract.
REASON_BREAKER_OPEN = "breaker_open"
REASON_PROBE_IN_FLIGHT = "probe_in_flight"
REASON_QUEUE_FULL = "queue_full"
REASON_QUEUE_WAIT_TIMEOUT = "queue_wait_timeout"
REASON_NOT_DRAINING = "not_draining"
REASON_NO_WORKER_PICKING_UP = "no_worker_picking_up"
REASON_WORKER_STUCK = "worker_stuck"
REASON_WORKER_DIED = "worker_died"
REASON_JOB_DEADLINE = "job_deadline"


@dataclass(frozen=True)
class Admission:
    """The breaker's verdict for one inbound request.

    ``probe`` marks the single half-open request: it is admitted while the
    breaker refuses everything else, and its outcome decides whether the
    breaker closes or re-opens with a longer backoff.
    """

    admitted: bool
    probe: bool = False
    reason: str | None = None
    state: str = CLOSED


class SearchBreaker:
    """Refuse-and-recover state machine for the search path.

    ``clock`` is injectable so the half-open timing can be tested without
    sleeping.
    """

    def __init__(
        self,
        settings: Settings,
        registry: Registry | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._clock = clock

        self._state: str = CLOSED
        self._reason: str | None = None
        self._opened_at: float = 0.0
        self._open_until: float = 0.0
        self._probe_in_flight: bool = False
        self._failures: int = 0

        # A floor on the backoff, not a floor on the setting: it stops a
        # misconfigured 0 from turning the breaker into a no-op that re-admits
        # everything immediately.
        self._base_backoff = max(0.1, float(settings.search_breaker_open_seconds))
        self._max_backoff = max(self._base_backoff, float(settings.search_breaker_max_open_seconds))
        self._backoff = self._base_backoff
        self._threshold = max(1, settings.search_breaker_failure_threshold)
        self._queue_limit = max(1, settings.max_search_queue)

    # -- introspection ------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def retry_after(self) -> int:
        """Whole seconds a refused caller should wait before trying again."""
        if self._state == CLOSED:
            return 1
        remaining = self._open_until - self._clock()
        return max(1, math.ceil(remaining)) if remaining > 0 else 1

    def snapshot(self) -> dict[str, object]:
        """State for ``/status``, ``/health`` and the gauges."""
        now = self._clock()
        refusing = self._state != CLOSED
        return {
            "state": self._state,
            "reason": self._reason,
            "open_for_seconds": round(now - self._opened_at, 3) if refusing else 0.0,
            "retry_in_seconds": round(max(0.0, self._open_until - now), 3) if refusing else 0.0,
            "consecutive_failures": self._failures,
            "failure_threshold": self._threshold,
            "backoff_seconds": self._backoff,
        }

    # -- admission ----------------------------------------------------------

    def admit(self, queue_depth: int = 0) -> Admission:
        """Decide whether to take one more job. Synchronous by design.

        ``queue_depth`` is passed in rather than read here so the caller does no
        awaiting on the refusal path - the whole point is to answer in
        milliseconds.
        """
        if queue_depth >= self._queue_limit:
            # Full. Refusing now is honest: promising a slot we cannot honour
            # is how a queue becomes a graveyard.
            return self._refuse(REASON_QUEUE_FULL)

        if self._state == CLOSED:
            return Admission(admitted=True, state=CLOSED)

        if self._state == OPEN and self._clock() >= self._open_until:
            # The backoff has elapsed: let exactly one request through to find
            # out whether the path recovered, and keep refusing the rest.
            self._state = HALF_OPEN
            self._probe_in_flight = True
            logger.info(
                "search breaker half-open after %s; admitting one probe request",
                self._reason,
            )
            return Admission(admitted=True, probe=True, state=HALF_OPEN)

        reason = REASON_PROBE_IN_FLIGHT if self._state == HALF_OPEN else REASON_BREAKER_OPEN
        return self._refuse(reason)

    def _refuse(self, reason: str) -> Admission:
        state = self._state
        self._count_rejection(reason)
        return Admission(admitted=False, reason=reason, state=state)

    def _count_rejection(self, reason: str) -> None:
        if self._registry is not None:
            self._registry.increment(
                "muninn_search_breaker_rejections_total", {"reason": reason}
            )

    # -- outcomes -----------------------------------------------------------

    def note_job_completed(self) -> None:
        """A job reached a definitive outcome, so the workers are draining.

        This is the only thing that closes a half-open breaker, and it is
        deliberately generous: a job that failed because every engine blocked
        us still proves the worker picked it up and released it. Engine health
        has its own circuit breaker; this one only cares about the queue.
        """
        self._failures = 0
        self._backoff = self._base_backoff
        self._probe_in_flight = False
        if self._state != CLOSED:
            logger.info("search breaker closed after a healthy job (was: %s)", self._reason)
        self._state = CLOSED
        self._reason = None
        self._open_until = 0.0

    def note_failure(self, reason: str) -> None:
        """Something went wrong that the queue could not absorb."""
        self._failures += 1
        failed_probe = self._probe_in_flight
        self._probe_in_flight = False
        if failed_probe or self._failures >= self._threshold:
            self.trip(reason)

    def observe(self, *, depth: int, stuck: bool, dispatcher_stalled: bool = False) -> None:
        """Supervisor tick.

        ``dispatcher_stalled`` is the one condition that needs no judgement
        call: a non-empty queue while *every* worker reports itself idle means no
        worker has taken a job, which is a broken dispatcher rather than a slow
        one. It cannot fire spuriously while the throttle wait is reported as
        its own state, which is why the worker reports it.
        """
        if dispatcher_stalled:
            self.trip(REASON_NO_WORKER_PICKING_UP)
            return
        if stuck:
            self.trip(REASON_NOT_DRAINING if depth > 0 else REASON_WORKER_STUCK)
            return
        if depth > 0:
            logger.debug("search queue draining: depth=%d", depth)

    def trip(self, reason: str) -> None:
        """Open the breaker (or extend an open one) with exponential backoff."""
        now = self._clock()
        duration = self._backoff
        if self._state == OPEN:
            # Already refusing: extend, never shorten.
            self._open_until = max(self._open_until, now + duration)
        else:
            self._state = OPEN
            self._opened_at = now
            self._open_until = now + duration
        self._reason = reason
        self._probe_in_flight = False
        self._backoff = min(self._max_backoff, duration * 2)

        if self._registry is not None:
            self._registry.increment("muninn_search_breaker_trips_total", {"reason": reason})
        logger.warning(
            "search breaker OPEN (%s): refusing new searches for %.0fs "
            "(next attempt %.0fs)",
            reason, duration, self._backoff,
        )
