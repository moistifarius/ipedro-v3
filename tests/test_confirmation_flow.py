"""Tests for the destructive-op confirmation keyboard."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers.admin import _confirmation_keyboard, build_router


def test_confirmation_keyboard_appends_confirm_cancel_suffixes():
    kb = _confirmation_keyboard("dsra:42")
    # One row, two buttons.
    assert len(kb.inline_keyboard) == 1
    row = kb.inline_keyboard[0]
    assert len(row) == 2
    confirm_btn, cancel_btn = row
    assert confirm_btn.callback_data == "dsra:42:confirm"
    assert cancel_btn.callback_data == "dsra:42:cancel"
    assert "YES" in confirm_btn.text.upper()
    assert "CANCEL" in cancel_btn.text.upper()


def test_confirmation_keyboard_with_deep_callback_prefix():
    """E.g. dse:reset:CHATID:USERID — confirm/cancel suffixes still work."""
    kb = _confirmation_keyboard("dse:reset:-1009876:315660812")
    row = kb.inline_keyboard[0]
    assert row[0].callback_data == "dse:reset:-1009876:315660812:confirm"
    assert row[1].callback_data == "dse:reset:-1009876:315660812:cancel"


def test_confirmation_yes_fires_after_arbitrary_delay():
    """Confirmation has no TTL; the callback dispatcher doesn't gate on time.

    We exercise the keyboard generator and the suffix layout — both contain
    no time-sensitive state.
    """
    kb1 = _confirmation_keyboard("dsra:7")
    kb2 = _confirmation_keyboard("dsra:7")
    # Two independently-generated keyboards have the same callback shape.
    assert kb1.inline_keyboard[0][0].callback_data == kb2.inline_keyboard[0][0].callback_data


# --- Regression tests for the cancel branches of destructive flows -------

@dataclass
class _RecordingDB:
    """Minimal DB stub that records execute() and fetchrow() calls so a
    test can assert no DELETE was emitted."""

    fetchrow_results: list = field(default_factory=list)
    fetchval_results: list = field(default_factory=list)
    executes: list[tuple] = field(default_factory=list)
    fetchrows: list[tuple] = field(default_factory=list)

    async def execute(self, query, *args):
        self.executes.append((query, args))
        return "UPDATE 0"

    async def fetchrow(self, query, *args):
        self.fetchrows.append((query, args))
        if not self.fetchrow_results:
            return None
        return self.fetchrow_results.pop(0)

    async def fetchval(self, query, *args):
        if not self.fetchval_results:
            return 0
        return self.fetchval_results.pop(0)


def _find_handler(router, name):
    for h in router.observers["callback_query"].handlers:
        if h.callback.__name__ == name:
            return h.callback
    raise AssertionError(f"handler {name} not registered")


def _fake_cb(data: str, *, admin_id: int = 1):
    """Build a fake CallbackQuery just rich enough for the cancel branches."""
    message = SimpleNamespace(
        edit_text=AsyncMock(),
        edit_reply_markup=AsyncMock(),
    )
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=admin_id),
        message=message,
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_dsra_cancel_does_not_fire_DELETE():
    """Tapping 'Cancel' on the bulk-wipe confirmation must NOT execute any
    DELETE — this is the core safety promise of the confirmation step."""
    db = _RecordingDB()
    rt = SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset({1})),
        db=db,
    )
    r = build_router(rt)
    on_dsr_reset_all = _find_handler(r, "on_dsr_reset_all")
    cb = _fake_cb("dsra:42:cancel")
    await on_dsr_reset_all(cb)
    # No SQL of any kind should have been executed.
    assert db.executes == []
    # And the user got a clear "cancelled" message.
    cb.message.edit_text.assert_awaited()
    body = cb.message.edit_text.await_args.args[0]
    assert "Cancelled" in body


@pytest.mark.asyncio
async def test_dse_reset_cancel_does_not_fire_DELETE():
    """Tapping 'Cancel' on the per-user row-reset confirmation must NOT
    execute any DELETE. The cancel branch re-renders the editor view via
    _render_duckstat_editor (a SELECT), so we filter to just DELETE
    statements when asserting."""
    # The editor render needs one fetchrow returning the user's stats.
    db = _RecordingDB(fetchrow_results=[{
        "display_name": "alice", "points": 10, "killed": 0, "befriended": 0,
        "misses": 0, "streak": 0, "best_streak": 0,
    }])
    rt = SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset({1})),
        db=db,
    )
    r = build_router(rt)
    on_dse = _find_handler(r, "on_dse")
    cb = _fake_cb("dse:reset:42:7:cancel")
    await on_dse(cb)
    # No DELETE statement should have run.
    delete_calls = [q for (q, _a) in db.executes if "DELETE" in q.upper()]
    assert delete_calls == []
