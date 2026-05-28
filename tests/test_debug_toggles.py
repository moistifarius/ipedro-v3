"""Tests for the admin-scoped duckhunt debug-toggle cache."""

from __future__ import annotations

import pytest

from ipedro.duckhunt import debug_toggles


@pytest.fixture(autouse=True)
def clean_cache():
    debug_toggles._reset_cache_for_tests()
    yield
    debug_toggles._reset_cache_for_tests()


class _FakeDB:
    """In-memory kv_store stand-in.

    The real kv module hits Postgres. We only need value-equivalent
    behavior for set/get/delete — the toggle module never hand-writes
    SQL, it only calls kv_get/kv_set/kv_delete.
    """

    def __init__(self):
        self.store: dict[str, str] = {}
        self.calls: list[tuple[str, str, str | None]] = []

    async def execute(self, query, *args):  # pragma: no cover - kv writes
        if "INSERT" in query.upper():
            key, value = args[0], args[1]
            self.store[key] = value
            self.calls.append(("set", key, value))
        elif "DELETE" in query.upper():
            key = args[0]
            self.store.pop(key, None)
            self.calls.append(("delete", key, None))
        return "OK"

    async def fetchval(self, query, *args):
        key = args[0]
        return self.store.get(key)

    async def fetchrow(self, query, *args):
        # kv_get uses fetchrow returning {"value": ...} | None.
        key = args[0]
        if key in self.store:
            return {"value": self.store[key]}
        return None


def test_is_on_returns_false_by_default():
    assert debug_toggles.is_on(123, "always_hit") is False
    assert debug_toggles.is_on(None, "always_hit") is False


@pytest.mark.asyncio
async def test_set_toggle_caches_and_persists():
    db = _FakeDB()
    await debug_toggles.set_toggle(db, 123, "always_hit", True)
    assert debug_toggles.is_on(123, "always_hit") is True
    # Persisted via kv_set.
    assert any(c[0] == "set" for c in db.calls)


@pytest.mark.asyncio
async def test_set_toggle_off_clears_cache_and_kv():
    db = _FakeDB()
    await debug_toggles.set_toggle(db, 123, "always_hit", True)
    await debug_toggles.set_toggle(db, 123, "always_hit", False)
    assert debug_toggles.is_on(123, "always_hit") is False
    assert any(c[0] == "delete" for c in db.calls)


@pytest.mark.asyncio
async def test_set_toggle_unknown_name_raises():
    db = _FakeDB()
    with pytest.raises(ValueError, match="unknown toggle"):
        await debug_toggles.set_toggle(db, 123, "not_a_real_toggle", True)


def test_all_for_returns_every_toggle_name():
    state = debug_toggles.all_for(123)
    assert set(state.keys()) == set(debug_toggles.DEBUG_TOGGLE_NAMES)
    assert all(v is False for v in state.values())


@pytest.mark.asyncio
async def test_toggles_are_per_admin_user():
    db = _FakeDB()
    await debug_toggles.set_toggle(db, 1, "always_hit", True)
    assert debug_toggles.is_on(1, "always_hit") is True
    assert debug_toggles.is_on(2, "always_hit") is False


@pytest.mark.asyncio
async def test_load_all_primes_cache_from_kv():
    db = _FakeDB()
    db.store["debug:always_hit:42"] = "1"
    db.store["debug:bypass_cooldowns:99"] = "1"
    await debug_toggles.load_all(db, [42, 99])
    assert debug_toggles.is_on(42, "always_hit") is True
    assert debug_toggles.is_on(99, "bypass_cooldowns") is True
    assert debug_toggles.is_on(42, "always_miss") is False
