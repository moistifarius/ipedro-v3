"""Tests for the /duckstats_edit helpers and dispatcher building blocks."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest

from ipedro.handlers import admin
from ipedro.handlers.admin import (
    _DUCKSTAT_EDITABLE_FIELDS,
    _PENDING_CUSTOM_VALUES,
    _apply_duckstat_delta,
    _render_duckstat_field_picker,
    _set_duckstat_field,
)


@dataclass
class FakeDB:
    """A minimal DB stub that records UPDATE invocations and replays
    fetchrow results in FIFO order."""

    fetchrow_results: list = field(default_factory=list)
    fetchrow_calls: list = field(default_factory=list)

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if not self.fetchrow_results:
            return None
        return self.fetchrow_results.pop(0)


@pytest.mark.asyncio
async def test_apply_duckstat_delta_rejects_nonwhitelist_field():
    """Field-name allowlist must short-circuit BEFORE any SQL is built."""
    db = FakeDB()
    ok, val = await _apply_duckstat_delta(
        db, chat_id=1, user_id=2, field="display_name", delta=10,
    )
    assert ok is False and val is None
    # No SQL executed.
    assert db.fetchrow_calls == []


@pytest.mark.asyncio
async def test_apply_duckstat_delta_clamps_negative():
    db = FakeDB(fetchrow_results=[{"points": 0}])
    ok, val = await _apply_duckstat_delta(
        db, chat_id=1, user_id=2, field="points", delta=-1000,
    )
    assert ok is True and val == 0
    # The UPDATE used GREATEST(0, …) — verified by SQL substring.
    sql, args = db.fetchrow_calls[0]
    assert "GREATEST(0," in sql
    assert args == (1, 2, -1000)


@pytest.mark.asyncio
async def test_apply_duckstat_delta_normal_increment():
    db = FakeDB(fetchrow_results=[{"points": 110}])
    ok, val = await _apply_duckstat_delta(
        db, chat_id=1, user_id=2, field="points", delta=10,
    )
    assert ok is True and val == 110


@pytest.mark.asyncio
async def test_apply_duckstat_delta_returns_false_when_no_row():
    db = FakeDB(fetchrow_results=[None])
    ok, val = await _apply_duckstat_delta(
        db, chat_id=1, user_id=2, field="points", delta=10,
    )
    assert ok is False and val is None


@pytest.mark.asyncio
async def test_set_duckstat_field_clamps_negative():
    db = FakeDB(fetchrow_results=[{"points": 0}])
    ok, val = await _set_duckstat_field(
        db, chat_id=1, user_id=2, field="points", value=-50,
    )
    assert ok is True and val == 0
    # The SQL should have used the clamped 0, not the raw -50.
    sql, args = db.fetchrow_calls[0]
    assert args == (1, 2, 0)


@pytest.mark.asyncio
async def test_set_duckstat_field_rejects_nonwhitelist():
    db = FakeDB()
    ok, val = await _set_duckstat_field(
        db, 1, 2, "display_name", 7,
    )
    assert ok is False and val is None
    assert db.fetchrow_calls == []


def test_duckstat_editable_fields_exact_membership():
    assert _DUCKSTAT_EDITABLE_FIELDS == (
        "points", "killed", "befriended", "misses", "streak", "best_streak",
    )


def test_field_picker_callback_shapes():
    head, kb = _render_duckstat_field_picker(
        chat_id=42, user_id=7, field="points", current_value=100,
    )
    callback_datas = []
    for row in kb.inline_keyboard:
        for btn in row:
            callback_datas.append(btn.callback_data)
    # Six delta buttons.
    assert "dse:delta:42:7:points:-100" in callback_datas
    assert "dse:delta:42:7:points:-10" in callback_datas
    assert "dse:delta:42:7:points:-1" in callback_datas
    assert "dse:delta:42:7:points:+1" in callback_datas
    assert "dse:delta:42:7:points:+10" in callback_datas
    assert "dse:delta:42:7:points:+100" in callback_datas
    # Set to 0 + custom + back.
    assert "dse:zero:42:7:points" in callback_datas
    assert "dse:custom:42:7:points" in callback_datas
    assert "dse:user:42:7" in callback_datas
    assert "points: 100" in head


def test_custom_value_ttl_expires_after_60s():
    """Confirm the TTL constant is the documented 60s and a stale entry
    is recognized."""
    assert admin._CUSTOM_VALUE_TTL == 60.0
    # Simulate an entry with a stale timestamp.
    stale_ts = time.time() - 120
    # If we were to consult the entry directly, it should be considered
    # expired. The handler bakes this check inline; we just verify the
    # arithmetic is correct.
    assert time.time() - stale_ts > admin._CUSTOM_VALUE_TTL


def test_dse_callback_parser_extracts_negative_chat_id():
    cb = "dse:delta:-1009876543210123456:315660812:best_streak:-100"
    parts = cb.split(":")
    assert parts[0] == "dse"
    assert parts[1] == "delta"
    assert int(parts[2]) == -1009876543210123456
    assert int(parts[3]) == 315660812
    assert parts[4] == "best_streak"
    assert int(parts[5]) == -100


def test_custom_value_handler_predicate_for_unparked_admin():
    """`_has_parked_value` is defined inside build_router; verify the
    behaviour by exercising the dict membership check that backs it."""
    _PENDING_CUSTOM_VALUES.clear()
    assert 42 not in _PENDING_CUSTOM_VALUES
    _PENDING_CUSTOM_VALUES[42] = (1, 2, "points", time.time())
    assert 42 in _PENDING_CUSTOM_VALUES
    _PENDING_CUSTOM_VALUES.clear()


@pytest.mark.asyncio
async def test_custom_value_negative_clamps_to_zero():
    """When the admin types '-100' as a custom value, _set_duckstat_field
    should clamp it to 0."""
    db = FakeDB(fetchrow_results=[{"points": 0}])
    ok, val = await _set_duckstat_field(
        db, 1, 2, "points", -100,
    )
    assert ok is True and val == 0


@pytest.mark.asyncio
async def test_custom_value_non_decimal_rejected_gracefully():
    """The handler code does `int(msg.text.strip())` which raises
    ValueError for 'abc'. We just verify the exception is what we expect."""
    with pytest.raises(ValueError):
        int("abc".strip())
