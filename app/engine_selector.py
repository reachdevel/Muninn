"""Engine selection strategies.

Three ways to answer "which engine serves this job?", because the right answer
depends on the pool you actually have:

``grouped`` (default)
    Round-robin inside the first group that has any usable engine, falling back
    to the next group. This keeps a flaky engine off the hot path without
    removing it: an engine in group 1 that blocks is quarantined and skipped,
    while group 2 is only touched once everything above it is out. It is the
    default because it degrades in the order you would choose by hand.

``round_robin``
    Plain rotation across every active engine, regardless of tier. Fairest, and
    the right answer when you trust every engine equally.

``priority``
    Try engines in a fixed order and fall through on failure, like a failover
    list. The order is ``SUPPORTED_ENGINES``. The right answer when some engines
    are strictly better than others.

Every strategy is a pure function of (active engines, groups, rotation position),
so the choice is auditable and testable; nothing here does I/O.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import SUPPORTED_ENGINES

logger = logging.getLogger(__name__)

STRATEGIES = ("grouped", "round_robin", "priority")
DEFAULT_STRATEGY = "grouped"


class AllEnginesUnavailableError(Exception):
    """No configured engine can serve the request."""


@dataclass
class Selection:
    """One strategy's answer."""

    engine: str
    strategy: str
    #: Which group the engine came from (0-based). Only meaningful for ``grouped``.
    group: int | None = None


def parse_groups(raw: str, engines: tuple[str, ...] = SUPPORTED_ENGINES) -> tuple[tuple[str, ...], ...]:
    """Parse ``a,b|c,d`` into ordered groups of known engines.

    Unknown names are dropped with a warning rather than raising: a typo in one
    name should not take the search path down. Engines the operator forgot to
    mention are appended to a final group, so adding one to the pool can never
    make it unreachable - the failure mode of "I edited the groups and now an
    engine never gets tried".
    """
    groups: list[list[str]] = []
    seen: set[str] = set()
    for chunk in (raw or "").split("|"):
        names = [n.strip().lower() for n in chunk.split(",") if n.strip()]
        members: list[str] = []
        for name in names:
            if name not in engines:
                logger.warning("search_engine_groups: unknown engine %r ignored", name)
                continue
            if name in seen:
                continue
            seen.add(name)
            members.append(name)
        if members:
            groups.append(members)

    leftovers = [name for name in engines if name not in seen]
    if leftovers:
        if groups:
            logger.warning(
                "search_engine_groups: %s not listed; trying them last",
                ", ".join(leftovers),
            )
            groups.append(leftovers)
        else:
            # Nothing valid configured - the whole pool in declared order.
            groups.append(list(leftovers))
    if not groups:
        raise ValueError("no engines available for selection")
    return tuple(tuple(g) for g in groups)


class EngineSelector:
    """Chooses the next engine, and remembers where it left off.

    Position is per strategy state, so switching ``SEARCH_STRATEGY`` at runtime
    cannot leave a stale rotation pointer pointing into an unrelated list.
    """

    def __init__(
        self,
        strategy: str = DEFAULT_STRATEGY,
        groups: tuple[tuple[str, ...], ...] | None = None,
        engines: tuple[str, ...] = SUPPORTED_ENGINES,
    ) -> None:
        name = (strategy or DEFAULT_STRATEGY).strip().lower()
        if name not in STRATEGIES:
            logger.warning(
                "unknown search strategy %r; using %s", strategy, DEFAULT_STRATEGY
            )
            name = DEFAULT_STRATEGY
        self.strategy = name
        self.groups = groups if groups is not None else (tuple(engines),)
        self._positions = [0] * len(self.groups)

    # -- introspection -------------------------------------------------------

    def describe(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "groups": [list(g) for g in self.groups],
        }

    def position(self, group: int) -> int:
        return self._positions[group] if group < len(self._positions) else 0

    # -- selection -----------------------------------------------------------

    def select(
        self,
        active: list[str],
        exclude: frozenset[str] = frozenset(),
    ) -> Selection:
        """Pick an engine, or raise :class:`AllEnginesUnavailableError`.

        ``exclude`` is how one job avoids retrying an engine that just failed:
        each attempt in a job passes the engines it has already tried, so the
        strategies do not have to encode retry semantics themselves.
        """
        usable = [name for name in active if name not in exclude]
        if not usable:
            raise AllEnginesUnavailableError(
                "no active engine left for this job"
            )

        if self.strategy == "priority":
            for name in self._ordered_engines():
                if name in usable:
                    return Selection(engine=name, strategy=self.strategy)
        elif self.strategy == "round_robin":
            return Selection(engine=self._rotate(self._ordered_engines(), usable),
                             strategy=self.strategy)

        # grouped: walk the groups left to right, rotating inside the first
        # usable one. Fallback to a flat rotation if the config does not cover
        # the active set at all.
        covered = [name for group in self.groups for name in group]
        ordered = covered + [n for n in self._ordered_engines() if n not in covered]
        for index, group in enumerate(self.groups):
            members = [name for name in group if name in usable]
            if not members:
                continue
            if index >= len(self._positions):
                self._positions.append(0)
            return Selection(
                engine=self._rotate_at(index, members),
                strategy=self.strategy,
                group=index,
            )
        return Selection(engine=self._rotate(ordered, usable), strategy=self.strategy)

    # -- internals -----------------------------------------------------------

    def _ordered_engines(self) -> list[str]:
        """Declaration order: SUPPORTED_ENGINES, i.e. the operator's list."""
        return list(SUPPORTED_ENGINES)

    def _rotate_at(self, group: int, usable: list[str]) -> str:
        position = self._positions[group] % len(usable)
        engine = usable[position]
        self._positions[group] = position + 1
        return engine

    def _rotate(self, ordered: list[str], usable: list[str]) -> str:
        """Round-robin over ``usable``, advancing from the last engine handed out."""
        if len(self._positions) < 2:
            self._positions.append(0)
        previous = self._ordered_engine
        if previous is not None and previous in usable:
            start = usable.index(previous) + 1
        else:
            start = self._positions[1]
        engine = usable[start % len(usable)]
        self._positions[1] = start % len(usable)
        self._ordered_engine = engine
        return engine

    _ordered_engine: str | None = None