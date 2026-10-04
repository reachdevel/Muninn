"""Engine pool manager and circuit breaker.

Owns the per-engine state, enforces the Round-Robin selection across *active*
(non-quarantined) engines, and implements the quarantine escalation policy:

* Fail #1 (429 / CAPTCHA)  -> ``quarantine_first_seconds`` (5 minutes by default).
* Fail #2+ (consecutive)   -> doubling per failure, capped at
  ``quarantine_escalated_seconds`` (30 minutes by default).

The cap is the point. The policy used to escalate straight to 12 hours, and
because the pool is small, several quarantines could take the service down to one
engine for the best part of a day - one bad engine, no capacity. Bounded
cooldowns keep the pool usable even when half of it is walled off.
Bounded cooldowns with frequent re-probes keep capacity, and the exponential
growth still means a persistently broken engine is left alone for a while.

Failures are also *classified* (see :data:`FAILURE_CLASSES`): a CAPTCHA, a DNS
blip, a changed page shape and a timeout are different problems and do not all
deserve the same cooldown.

Quarantined engines are dropped from rotation; if every engine is quarantined
the manager raises :class:`AllEnginesQuarantinedError` and the API returns 503.

State is durable. The manager takes an optional
:class:`~app.engine_state_store.EngineStateStore`; when present, counters and
quarantine deadlines are restored on :meth:`restore` and written on every
report. That matters because a restart is exactly when you do *not* want to
forget that an engine just blocked you.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.config import SUPPORTED_ENGINES, Settings
from app.engine_selector import (
    AllEnginesUnavailableError,
    EngineSelector,
    parse_groups,
)
from app.engine_state_store import EngineStateStore
from app.models import EngineState
from ops.metrics import Registry

logger = logging.getLogger(__name__)

#: The failure classes an engine outcome can fall into. Kept closed because the
#: class is reported in ``/status`` and is an operator-facing contract.
FAILURE_CLASSES = ("block", "timeout", "network", "parse")

#: How much of the base cooldown each class earns.
#:
#: A block/challenge is the engine telling us to go away, so it earns the full
#: cooldown. A timeout, a network error or a page whose shape changed are weaker
#: signals: they are often transient, and quarantining hard for them throws away
#: pool capacity for nothing.
FAILURE_CLASS_WEIGHTS: dict[str, float] = {
    "block": 1.0,
    "timeout": 0.5,
    "network": 0.5,
    "parse": 0.5,
}

# Reason substrings that pin a report to a class. Matched case-insensitively as
# a substring so "HTTP 429", "captcha-redirect" and "job-deadline" all land.
_FAILURE_CLASS_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("timeout", ("timeout", "timed out", "deadline", "budget")),
    ("network", ("network", "dns", "connection", "reset", "refused", "econn", "unreachable")),
    ("parse", ("parse", "schema", "no-results", "no results", "unexpected-html")),
)

#: What a reason means when nothing more specific matches: the engine gave us
#: nothing usable, which for a residential IP is what being filtered looks like.
DEFAULT_FAILURE_CLASS = "block"


def classify_failure(reason: str) -> str:
    """Map a failure reason onto one of :data:`FAILURE_CLASSES`."""
    lowered = reason.lower()
    for failure_class, hints in _FAILURE_CLASS_HINTS:
        if any(hint in lowered for hint in hints):
            return failure_class
    return DEFAULT_FAILURE_CLASS


class AllEnginesQuarantinedError(Exception):
    """Every engine in the pool is currently under quarantine."""


class EngineManager:
    """Round-Robin engine rotator with quarantine/circuit-breaker logic."""

    def __init__(
        self,
        settings: Settings,
        store: EngineStateStore | None = None,
        registry: Registry | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._registry = registry
        self._states: dict[str, EngineState] = {
            name: EngineState(name=name) for name in SUPPORTED_ENGINES
        }
        # Selection policy lives in EngineSelector; the manager owns the state it
        # reads (quarantine) and the rotation position it advances.
        self._selector = EngineSelector(
            strategy=settings.search_strategy,
            groups=parse_groups(settings.search_engine_groups),
        )
        self._lock = asyncio.Lock()

    # -- persistence ---------------------------------------------------------

    async def restore(self) -> int:
        """Reload persisted counters/quarantines. Returns rows restored.

        Engines with no stored row keep their fresh defaults. A stored
        quarantine whose deadline has already passed is applied as a *completed*
        level so the next failure escalates immediately, matching the in-memory
        behaviour after a cooldown expires.
        """
        if self._store is None:
            return 0
        try:
            rows = await self._store.load()
        except Exception:  # pragma: no cover - never block startup on telemetry
            logger.warning("could not restore engine state", exc_info=True)
            return 0
        restored = 0
        for name, fails, successes, total, level, until in rows:
            state = self._states.get(name)
            if state is None:
                continue
            expired = until is not None and until <= time.time()
            state.fail_count = fails
            state.success_count = successes
            state.total_requests = total
            state.quarantine_level = level if (until is not None and not expired) else 0
            state.quarantined_until = None if (until is None or expired) else until
            restored += 1
        if restored:
            quarantined = [n for n, s in self._states.items() if not s.active]
            logger.info(
                "restored engine state for %d engine(s); still quarantined: %s",
                restored, quarantined or "none",
            )
        return restored

    async def _persist(self, state: EngineState) -> None:
        if self._store is None:
            return
        try:
            await self._store.save(
                state.name,
                state.fail_count,
                state.success_count,
                state.total_requests,
                state.quarantine_level,
                state.quarantined_until,
            )
        except Exception:  # pragma: no cover - telemetry must not fail a search
            logger.warning("could not persist engine state for %s", state.name, exc_info=True)

    # -- introspection ------------------------------------------------------

    @property
    def engines(self) -> tuple[str, ...]:
        return SUPPORTED_ENGINES

    def active_engines(self) -> list[str]:
        """Names of engines currently eligible for rotation."""
        return [name for name, st in self._states.items() if st.active]

    def is_available(self, engine: str) -> bool:
        """Whether ``engine`` may serve a request right now.

        Distinct from rotation: a *pinned* request cannot silently fall back to
        another engine, so the caller needs to be told when the one it named is
        out rather than waiting for an answer that was never coming.
        """
        state = self._states.get(engine)
        return state is not None and state.active

    async def status(self) -> dict[str, dict]:
        """Full state snapshot for ``GET /status`` and ``GET /health``."""
        async with self._lock:
            return {name: st.to_dict() for name, st in self._states.items()}

    # -- selection ----------------------------------------------------------

    async def resolve_engine(
        self, requested: str | None = None, exclude: frozenset[str] = frozenset()
    ) -> str:
        """Pick the engine for the next attempt of a job.

        * If ``requested`` names an active engine, use it directly: a pinned
          request must not be quietly rotated onto a different index.
        * Otherwise apply the configured strategy (``grouped`` by default) across
          the active engines, skipping any in ``exclude`` - that is how one job
          tries each engine at most once.
        * Raise :class:`AllEnginesQuarantinedError` when nothing is left.
        """
        async with self._lock:
            active = [name for name, st in self._states.items() if st.active]
            if not active:
                raise AllEnginesQuarantinedError()
            if requested is not None and requested in active:
                return requested
            try:
                return self._selector.select(active, exclude).engine
            except AllEnginesUnavailableError as exc:
                raise AllEnginesQuarantinedError() from exc

    def selection_policy(self) -> dict[str, object]:
        """The configured strategy and groups, for ``/status``."""
        return self._selector.describe()

    # -- outcomes -----------------------------------------------------------

    async def report_success(self, engine: str) -> None:
        """Record a successful search; resets the consecutive failure counter."""
        async with self._lock:
            st = self._states[engine]
            st.fail_count = 0
            st.quarantine_level = 0
            st.quarantined_until = None
            st.success_count += 1
            st.total_requests += 1
            logger.debug("engine=%s ok (successes=%d)", engine, st.success_count)
        self._count(engine, "success")
        await self._persist(self._states[engine])

    async def report_failure(self, engine: str, reason: str) -> None:
        """Record a failed search against ``engine`` and quarantine it.

        The cooldown is ``quarantine_first_seconds`` scaled by the failure class
        and doubled per consecutive failure, capped at
        ``quarantine_escalated_seconds``. Only this engine is affected: one bad
        engine must never take the whole pool out. That matters more than it used
        to - Yandex and Qwant sit behind SmartCaptcha and DataDome, so they are
        expected to spend a good share of their time quarantined.
        """
        failure_class = classify_failure(reason)
        async with self._lock:
            st = self._states[engine]
            st.total_requests += 1
            st.fail_count += 1
            st.last_failure_class = failure_class

            duration = self._cooldown_seconds(st.fail_count, failure_class)
            st.quarantine_level = 1 if st.fail_count == 1 else 2
            st.quarantined_until = time.time() + duration
            if st.success_count == 0 and st.fail_count >= 2:
                # Worth saying out loud: a longer cooldown cannot fix an engine
                # that has never once answered. Resetting its deadline without
                # diagnosing just re-trips it.
                logger.warning(
                    "engine=%s has failed %d times (%s) and never succeeded; a "
                    "longer quarantine will not help - check the parser and the "
                    "engine URL before clearing the cooldown",
                    engine, st.fail_count, failure_class,
                )
            logger.warning(
                "engine=%s failed (%s/%s) -> quarantine level %d for %ds",
                engine, failure_class, reason, st.quarantine_level, duration,
            )
        self._count(engine, failure_class)
        await self._persist(st)

    def _count(self, engine: str, outcome: str) -> None:
        """Per-engine outcome counter, so ``/metrics`` can answer which engine.

        Failure classes are labels here: "google: 9 blocks, 0 successes" is a
        parser problem and "google: 9 timeouts" is a network one, and neither is
        visible in a single success/fail number.
        """
        if self._registry is not None:
            self._registry.increment(
                "muninn_engine_total", {"engine": engine, "outcome": outcome}
            )

    def _cooldown_seconds(self, fail_count: int, failure_class: str) -> int:
        """Bounded exponential backoff for one engine's consecutive failures.

        ``0`` stays ``0``: a configured base or cap of zero means "do not
        quarantine", which is a legitimate (test/bench) setting and not this
        policy's business to overrule.
        """
        base = float(self._settings.quarantine_first_seconds)
        weight = FAILURE_CLASS_WEIGHTS.get(failure_class, 1.0)
        escalations = max(0, fail_count - 1)
        duration = base * weight * (2**escalations)
        cap = float(self._settings.quarantine_escalated_seconds)
        return max(0, int(min(cap, duration)))

