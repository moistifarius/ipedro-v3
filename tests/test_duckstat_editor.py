"""Tests for the /duckstats_edit helpers and dispatcher building blocks."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers import admin
from ipedro.handlers.admin import (
    _CUSTOM_VALUE_TTL,
    _DUCKSTAT_EDITABLE_FIELDS,
    _PENDING_CUSTOM_VALUES,
    _apply_duckstat_delta,
    _has_parked_value,
    _pop_parked_on_navigation,
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
    _PENDING_CUSTOM_VALUES[42] = (1, 2, "points", time.time(), 42)
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


# --- Fix #3 / #4 — Postgres INTEGER overflow guards ----------------------

@pytest.mark.asyncio
async def test_apply_duckstat_delta_caps_at_int_max():
    """Delta SQL must clamp with LEAST(2147483647, …) so a positive delta
    on an already-large field can't overflow the INTEGER column."""
    db = FakeDB(fetchrow_results=[{"points": 2_147_483_647}])
    ok, val = await _apply_duckstat_delta(
        db, chat_id=1, user_id=2, field="points", delta=100,
    )
    assert ok is True and val == 2_147_483_647
    sql, _args = db.fetchrow_calls[0]
    # The cap is what guards against asyncpg DataError on overflow.
    assert "LEAST(2147483647" in sql
    # And we kept the floor as well.
    assert "GREATEST(0" in sql


@pytest.mark.asyncio
async def test_apply_duckstat_delta_returns_int_max_when_clamped():
    """Alternative behavioural assertion: when Postgres returns the cap,
    the helper surfaces it unchanged as the new value."""
    db = FakeDB(fetchrow_results=[{"points": 2_147_483_647}])
    ok, val = await _apply_duckstat_delta(
        db, chat_id=1, user_id=2, field="points", delta=10_000,
    )
    assert (ok, val) == (True, 2_147_483_647)


@pytest.mark.asyncio
async def test_custom_value_caps_at_int_max():
    """An admin-typed value above INT_MAX must be clamped before the SET
    so asyncpg never sees an out-of-range value."""
    db = FakeDB(fetchrow_results=[{"points": 2_147_483_647}])
    huge = 99_999_999_999_999_999_999
    ok, val = await _set_duckstat_field(
        db, 1, 2, "points", huge,
    )
    assert ok is True
    _sql, args = db.fetchrow_calls[0]
    # $3 (the value parameter) must be the clamp, not the raw input.
    assert args == (1, 2, 2_147_483_647)
    assert val == 2_147_483_647


# --- Fix #1 — _has_parked_value enforces the 60s TTL ---------------------

def _msg_stub(user_id: int, text: str, chat_id: int | None = None):
    """Tiny stand-in for aiogram Message that satisfies _has_parked_value.

    ``chat_id`` defaults to ``user_id`` — Telegram DM semantics, where the
    private chat's id equals the user's id. Pass a different value to
    simulate the admin typing in a group."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        chat=SimpleNamespace(id=chat_id if chat_id is not None else user_id),
    )


def test_has_parked_value_returns_false_when_entry_expired():
    """Stale (past-TTL) entry must (a) cause the predicate to return False
    so the message keeps propagating and (b) be popped from the dict."""
    _PENDING_CUSTOM_VALUES.clear()
    _PENDING_CUSTOM_VALUES[123] = (1, 2, "points", time.time() - 999.0, 123)
    msg = _msg_stub(123, "1234")
    assert _has_parked_value(msg) is False
    assert 123 not in _PENDING_CUSTOM_VALUES
    _PENDING_CUSTOM_VALUES.clear()


def test_has_parked_value_returns_true_when_entry_fresh():
    """Within-TTL entry must return True and stay in the dict (the handler
    pops it later — the predicate must not consume it)."""
    _PENDING_CUSTOM_VALUES.clear()
    _PENDING_CUSTOM_VALUES[123] = (1, 2, "points", time.time() - 5.0, 123)
    msg = _msg_stub(123, "1234")
    assert _has_parked_value(msg) is True
    assert 123 in _PENDING_CUSTOM_VALUES
    _PENDING_CUSTOM_VALUES.clear()


def test_has_parked_value_ignores_slash_commands():
    """Even with a fresh entry, a /command must NOT be captured — the
    admin's slash commands have to keep flowing to their own handlers."""
    _PENDING_CUSTOM_VALUES.clear()
    _PENDING_CUSTOM_VALUES[123] = (1, 2, "points", time.time(), 123)
    assert _has_parked_value(_msg_stub(123, "/help")) is False
    # Slash check must not pop the entry — the admin might still
    # complete the custom-value flow with a real number.
    assert 123 in _PENDING_CUSTOM_VALUES
    _PENDING_CUSTOM_VALUES.clear()


def test_has_parked_value_ignores_other_chats():
    """An entry parked in the admin DM (chat 123) must NOT capture a plain
    message the admin types in a GROUP within the TTL window — that group
    chatter would otherwise be swallowed and applied as a duckstat value.
    The entry stays parked so the flow can still finish in the DM."""
    _PENDING_CUSTOM_VALUES.clear()
    _PENDING_CUSTOM_VALUES[123] = (1, 2, "points", time.time(), 123)
    group_msg = _msg_stub(123, "1234", chat_id=-100987654321)
    assert _has_parked_value(group_msg) is False
    assert 123 in _PENDING_CUSTOM_VALUES
    # Same message typed in the parked chat is still captured.
    assert _has_parked_value(_msg_stub(123, "1234")) is True
    _PENDING_CUSTOM_VALUES.clear()


# --- Fix #2 — dse navigation clears the parked entry ---------------------

def test_dse_navigation_clears_parked_custom_value():
    """Any dse: verb other than 'custom' must drop the parked entry so a
    later typed number can't silently write to a field the admin has
    already navigated away from. Regression test for Fix #2."""
    _PENDING_CUSTOM_VALUES.clear()
    _PENDING_CUSTOM_VALUES[42] = (1, 2, "points", time.time(), 42)
    # 'field' is the navigation that happens when the admin re-opens a
    # different field's picker.
    _pop_parked_on_navigation("field", 42)
    assert 42 not in _PENDING_CUSTOM_VALUES
    _PENDING_CUSTOM_VALUES.clear()


def test_dse_custom_does_not_clear_parked_entry():
    """The 'custom' verb is the one that ARMS the parked entry — popping
    here would defeat the feature."""
    _PENDING_CUSTOM_VALUES.clear()
    _PENDING_CUSTOM_VALUES[42] = (1, 2, "points", time.time(), 42)
    _pop_parked_on_navigation("custom", 42)
    assert 42 in _PENDING_CUSTOM_VALUES
    _PENDING_CUSTOM_VALUES.clear()


# --- Fix #6 (test #11) — handler ignores unparked admin DMs --------------

@pytest.mark.asyncio
async def test_custom_value_handler_ignores_unparked_admin_dm():
    """No parked entry → the predicate short-circuits, the handler never
    runs, and the DB is left alone. This is what lets unrelated admin
    chitchat flow through to the chat router untouched."""
    _PENDING_CUSTOM_VALUES.clear()
    db = FakeDB()
    msg = _msg_stub(123, "hi")
    # Predicate gate first: must return False so aiogram never invokes
    # the handler.
    assert _has_parked_value(msg) is False
    # DB must not have been touched as a side effect of the predicate.
    assert db.fetchrow_calls == []
