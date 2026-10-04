"""Unit tests for the EngineManager circuit breaker and Round-Robin logic."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from app.cache import SearchCache
from app.config import SUPPORTED_ENGINES, Settings
from app.engine_manager import AllEnginesQuarantinedError, EngineManager
from app.engine_state_store import EngineStateStore


@pytest.fixture
def settings() -> Settings:
    return Settings(
        quarantine_first_seconds=1_800,
        quarantine_escalated_seconds=43_200,
    )


@pytest.fixture
def manager(settings: Settings) -> EngineManager:
    return EngineManager(settings)


@pytest.fixture
def rr_manager(settings: Settings) -> EngineManager:
    """A manager explicitly in round-robin mode.

    The default strategy is `grouped`, which deliberately does not produce a
    flat rotation. The rotation tests are about the rotation, so they ask for it
    by name rather than inheriting whatever the default happens to be.
    """
    return EngineManager(replace(settings, search_strategy="round_robin"))


def _expire_all(manager: EngineManager) -> None:
    """Simulate every quarantine cooldown expiring (move clock forward)."""
    for st in manager._states.values():
        st.quarantined_until = min(st.quarantined_until or 0, time.time() - 1)


# --------------------------------------------------------------------------- initial state


async def test_all_engines_start_active(manager: EngineManager) -> None:
    assert set(manager.active_engines()) == set(SUPPORTED_ENGINES)
    status = await manager.status()
    assert all(v["status"] == "active" for v in status.values())


# --------------------------------------------------------------------------- quarantines


async def test_first_failure_quarantines_the_base_cooldown(manager: EngineManager) -> None:
    await manager.report_failure("google", "429")
    st = manager._states["google"]
    assert st.fail_count == 1
    assert st.quarantine_level == 1
    assert st.active is False
    assert st.remaining_cooldown() <= 1_800
    # engine is excluded from rotation
    assert "google" not in manager.active_engines()
    # others remain available
    assert set(manager.active_engines()) == set(SUPPORTED_ENGINES) - {"google"}


async def test_second_consecutive_failure_escalates_beyond_the_base(
    manager: EngineManager,
) -> None:
    await manager.report_failure("bing", "captcha")
    _expire_all(manager)  # cooldown elapses...
    await manager.report_failure("bing", "429")  # ...and it fails again on first retry
    st = manager._states["bing"]
    assert st.fail_count == 2
    assert st.quarantine_level == 2
    assert st.remaining_cooldown() <= 43_200
    # severity kicked in: the cooldown grew past the base
    assert st.remaining_cooldown() > 1_800


async def test_the_cooldown_is_capped_however_many_times_an_engine_fails(
    manager: EngineManager,
) -> None:
    """Bounded escalation.

    Doubling without a ceiling is what turned three failures into three engines
    out of a four-engine pool for twelve hours.
    """
    for _ in range(10):
        await manager.report_failure("mojeek", "captcha")
        _expire_all(manager)
    st = manager._states["mojeek"]
    assert st.fail_count == 10
    assert st.remaining_cooldown() <= 43_200


async def test_success_resets_failure_counter(manager: EngineManager) -> None:
    await manager.report_failure("ddg", "captcha")
    await manager.report_success("ddg")  # impossible in reality while quarantined,
    # but models "success after cooldown" reset semantics
    st = manager._states["ddg"]
    assert st.fail_count == 0
    assert st.quarantine_level == 0
    assert st.active is True
    assert st.success_count == 1


async def test_escalation_requires_consecutive_failures(manager: EngineManager) -> None:
    # failure -> success -> failure must stay at the first quarantine level
    await manager.report_failure("mojeek", "429")
    await manager.report_success("mojeek")
    await manager.report_failure("mojeek", "429")
    st = manager._states["mojeek"]
    assert st.fail_count == 1
    assert st.quarantine_level == 1
    assert st.remaining_cooldown() <= 1_800


async def test_all_quarantined_raises(manager: EngineManager) -> None:
    for engine in SUPPORTED_ENGINES:
        await manager.report_failure(engine, "captcha")
    assert manager.active_engines() == []
    with pytest.raises(AllEnginesQuarantinedError):
        await manager.resolve_engine()


# --------------------------------------------------------------------------- round robin


async def test_round_robin_over_active_engines(rr_manager: EngineManager) -> None:
    count = 2 * len(SUPPORTED_ENGINES)
    picked = [await rr_manager.resolve_engine() for _ in range(count)]
    # two full cycles in declaration order, whichever engines are in the pool
    assert picked == list(SUPPORTED_ENGINES) * 2


async def test_round_robin_skips_quarantined(rr_manager: EngineManager) -> None:
    await rr_manager.report_failure("google", "captcha")
    picked = [
        await rr_manager.resolve_engine()
        for _ in range(2 * (len(SUPPORTED_ENGINES) - 1))
    ]
    assert "google" not in picked
    assert len(picked) == len(set(picked)) * 2, "each active engine is used equally"


async def test_requested_active_engine_is_honoured(manager: EngineManager) -> None:
    assert await manager.resolve_engine("bing") == "bing"


async def test_requested_quarantined_engine_is_ignored(manager: EngineManager) -> None:
    await manager.report_failure("ddg", "429")
    picked = await manager.resolve_engine("ddg")
    assert picked != "ddg"

# --------------------------------------------------------------- durable state


class ExplodingStore:
    """A store that always fails, to prove telemetry cannot break a search."""

    async def load(self) -> list:
        raise RuntimeError("disk on fire")

    async def save(self, *args, **kwargs) -> None:
        raise RuntimeError("disk on fire")


async def test_quarantine_survives_a_restart(tmp_path, settings: Settings) -> None:
    """Circuit-breaker state must outlive the process.

    A restart is exactly when you do not want to forget that an engine just
    blocked you, so the manager persists counters and deadlines to SQLite and
    reloads them on startup.
    """
    db_path = str(tmp_path / "state.db")

    cache_a = SearchCache(db_path, ttl_seconds=60)
    await cache_a.connect()
    mgr_a = EngineManager(settings, store=EngineStateStore(cache_a.connection))
    await mgr_a.report_failure("google", "captcha")
    await mgr_a.report_failure("bing", "429")
    await cache_a.close()

    cache_b = SearchCache(db_path, ttl_seconds=60)
    await cache_b.connect()
    mgr_b = EngineManager(settings, store=EngineStateStore(cache_b.connection))
    restored = await mgr_b.restore()
    await cache_b.close()

    assert restored == 2
    active = mgr_b.active_engines()
    assert "google" not in active
    assert "bing" not in active
    assert set(active) == set(SUPPORTED_ENGINES) - {"google", "bing"}


async def test_expired_quarantine_is_restored_as_usable(tmp_path) -> None:
    """A cooldown that already elapsed must not come back as a live block."""
    settings = Settings(quarantine_first_seconds=0, quarantine_escalated_seconds=0)
    db_path = str(tmp_path / "state.db")

    cache_a = SearchCache(db_path, ttl_seconds=60)
    await cache_a.connect()
    mgr_a = EngineManager(settings, store=EngineStateStore(cache_a.connection))
    await mgr_a.report_failure("google", "captcha")
    await cache_a.close()

    cache_b = SearchCache(db_path, ttl_seconds=60)
    await cache_b.connect()
    mgr_b = EngineManager(settings, store=EngineStateStore(cache_b.connection))
    await mgr_b.restore()
    await cache_b.close()

    assert "google" in mgr_b.active_engines()


async def test_store_failure_never_breaks_a_search(settings: Settings) -> None:
    mgr = EngineManager(settings, store=ExplodingStore())
    await mgr.report_success("google")  # must not raise
    assert "google" in mgr.active_engines()


async def test_restore_survives_a_broken_store(settings: Settings) -> None:
    mgr = EngineManager(settings, store=ExplodingStore())
    assert await mgr.restore() == 0
