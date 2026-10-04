"""Engine selection strategies.

The pool does not treat all engines equally — some are reliable, some are walled
off from a given IP — so "which engine next" is a policy decision, and this makes
it three explicit, testable policies instead of one hardcoded rotation.
"""

from __future__ import annotations

import pytest

from app.config import SUPPORTED_ENGINES, Settings
from app.engine_manager import AllEnginesQuarantinedError, EngineManager
from app.engine_selector import (
    DEFAULT_STRATEGY,
    AllEnginesUnavailableError,
    EngineSelector,
    parse_groups,
)

POOL = ("google", "bing", "ddg", "mojeek", "brave", "ecosia", "yahoo")
GROUPS = (("ddg", "brave", "bing", "yahoo", "google"), ("mojeek", "ecosia"))


def selector(strategy: str = "grouped", groups=GROUPS) -> EngineSelector:
    return EngineSelector(strategy=strategy, groups=tuple(groups), engines=POOL)


def manager(strategy: str = "grouped", groups: str = "ddg,brave|mojeek,ecosia") -> EngineManager:
    return EngineManager(
        Settings(search_strategy=strategy, search_engine_groups=groups)
    )


# ------------------------------------------------------------------- group parsing


def test_groups_are_parsed_left_to_right() -> None:
    assert parse_groups("ddg,brave|ecosia", POOL)[:2] == (("ddg", "brave"), ("ecosia",))


def test_unknown_engine_names_are_dropped_not_fatal() -> None:
    groups = parse_groups("ddg,altavista|ecosia", POOL)
    listed = [n for group in groups for n in group]
    assert groups[0] == ("ddg",)          # altavista ignored, not fatal
    assert "ecosia" in groups[1]          # ...and the rest of the pool is kept
    assert "altavista" not in listed


def test_an_engine_missing_from_the_groups_is_still_reachable() -> None:
    """A forgotten name must not make an engine unreachable.

    The failure mode of hand-edited group config is silence: an engine nobody
    mentions is simply never tried, and it looks like that engine is broken.
    """
    groups = parse_groups("ddg,brave", POOL)
    listed = {name for group in groups for name in group}
    assert listed == set(POOL)
    assert groups[0] == ("ddg", "brave")
    assert groups[-1] == tuple(n for n in POOL if n not in {"ddg", "brave"})


def test_duplicates_across_groups_are_collapsed() -> None:
    groups = parse_groups("ddg,brave|ddg,ecosia", POOL)
    assert groups[:2] == (("ddg", "brave"), ("ecosia",))
    assert sum(1 for g in groups for n in g if n == "ddg") == 1


def test_empty_group_config_falls_back_to_the_whole_pool() -> None:
    assert parse_groups("", POOL) == (POOL,)


def test_an_invalid_strategy_falls_back_to_the_default(caplog) -> None:
    assert EngineSelector(strategy="random", engines=POOL).strategy == DEFAULT_STRATEGY


# ------------------------------------------------------------------------- grouped


def test_grouped_prefers_the_first_group_and_rotates_inside_it() -> None:
    s = selector("grouped")
    picks = [s.select(list(POOL)).engine for _ in range(6)]
    assert set(picks) == set(GROUPS[0])
    # ...and it really rotates rather than hammering one engine.
    assert len(set(picks)) == len(GROUPS[0])


def test_grouped_falls_back_to_the_next_group_only_when_needed() -> None:
    s = selector("grouped")

    # One engine left in group 1: group 2 is not touched at all.
    assert s.select(["ddg"]).engine == "ddg"
    assert s.select(["brave"]).engine == "brave"

    # Only group 2 usable: it is used, and it rotates inside itself.
    picks = {s.select(["mojeek", "ecosia"]).engine for _ in range(4)}
    assert picks == set(GROUPS[1])


def test_grouped_reports_which_group_it_used() -> None:
    s = selector("grouped")
    assert s.select(list(POOL)).group == 0
    assert s.select(["ecosia"]).group == 1


def test_grouped_ignores_excluded_engines() -> None:
    s = selector("grouped")
    picked = s.select(list(POOL), exclude=frozenset(GROUPS[0][:4])).engine
    assert picked == "google"


def test_grouped_raises_when_everything_is_excluded() -> None:
    s = selector("grouped")
    with pytest.raises(AllEnginesUnavailableError):
        s.select(list(POOL), exclude=frozenset(POOL))


# -------------------------------------------------------------------- round robin


def test_round_robin_rotates_across_every_active_engine() -> None:
    s = selector("round_robin")
    picks = [s.select(list(POOL)).engine for _ in range(len(POOL))]
    assert set(picks) == set(POOL)


def test_round_robin_survives_an_engine_dropping_out() -> None:
    """The fairness bug this replaced: a counter modulo a shrinking list skips."""
    s = selector("round_robin")
    first = s.select(list(POOL)).engine
    remaining = [n for n in POOL if n != first]
    assert s.select(remaining).engine == remaining[0]


# ----------------------------------------------------------------------- priority


def test_priority_always_returns_the_first_available_engine() -> None:
    s = selector("priority")
    assert s.select(list(POOL)).engine == POOL[0]
    assert s.select(list(POOL)).engine == POOL[0]


def test_priority_falls_through_to_the_next_when_the_first_is_out() -> None:
    s = selector("priority")
    assert s.select(["bing", "ddg"]).engine == "bing"
    assert s.select(["ddg", "mojeek"]).engine == "ddg"


# ------------------------------------------------------------------- via the manager


async def test_the_manager_applies_the_configured_strategy() -> None:
    grouped = manager("grouped")
    first_group = {n for n in SUPPORTED_ENGINES if n in GROUPS[0]}
    for _ in range(6):
        assert await grouped.resolve_engine() in first_group

    priority = manager("priority")
    assert await priority.resolve_engine() == SUPPORTED_ENGINES[0]

    rr = manager("round_robin")
    picks = {await rr.resolve_engine() for _ in range(len(SUPPORTED_ENGINES))}
    assert picks == set(SUPPORTED_ENGINES)


async def test_the_manager_reports_its_policy() -> None:
    policy = manager("grouped").selection_policy()
    assert policy["strategy"] == "grouped"
    assert policy["groups"][0][0] == "ddg"


async def test_a_job_never_retries_the_same_engine() -> None:
    """exclude is what makes a retry a retry."""
    m = manager("round_robin")
    tried: set[str] = set()
    for _ in range(3):
        engine = await m.resolve_engine(exclude=frozenset(tried))
        assert engine not in tried
        tried.add(engine)


async def test_resolve_engine_reports_an_empty_pool() -> None:
    m = manager("priority")
    for engine in SUPPORTED_ENGINES:
        await m.report_failure(engine, "captcha")
    with pytest.raises(AllEnginesQuarantinedError):
        await m.resolve_engine()


async def test_a_pinned_engine_still_wins_over_the_strategy() -> None:
    """Rotation policy must never quietly answer a pinned query with another index."""
    m = manager("priority")
    assert await m.resolve_engine("ecosia") == "ecosia"
