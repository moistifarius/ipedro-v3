"""Reminder delivery: a reminder is only marked fired after a successful send.

Regression: a transient Telegram failure used to permanently mark the
reminder fired anyway — the user's reminder silently vanished.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError

from ipedro.reminders import parse_duration, run_reminders_loop


def _db(due_row):
    return SimpleNamespace(
        fetch=AsyncMock(return_value=[due_row] if due_row else []),
        execute=AsyncMock(),
    )


_ROW = {"id": 11, "chat_id": 42, "user_id": 7, "text": "take the tea off"}


@pytest.mark.asyncio
async def test_successful_send_marks_fired():
    stop = asyncio.Event()

    async def send(chat_id, body):
        stop.set()
        return SimpleNamespace(message_id=5)

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    db = _db(_ROW)
    await run_reminders_loop(bot, db, stop)
    db.execute.assert_awaited_once_with(
        "UPDATE reminders SET fired = TRUE WHERE id = $1", 11,
    )


@pytest.mark.asyncio
async def test_transient_failure_keeps_reminder_unfired():
    stop = asyncio.Event()

    async def send(chat_id, body):
        stop.set()
        raise RuntimeError("network blip")

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    db = _db(_ROW)
    await run_reminders_loop(bot, db, stop)
    db.execute.assert_not_awaited()   # NOT marked — retries next tick


@pytest.mark.asyncio
async def test_permanent_failure_drops_reminder():
    stop = asyncio.Event()

    async def send(chat_id, body):
        stop.set()
        raise TelegramForbiddenError(
            method=SimpleNamespace(), message="Forbidden: bot was kicked",
        )

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    db = _db(_ROW)
    await run_reminders_loop(bot, db, stop)
    db.execute.assert_awaited_once_with(
        "UPDATE reminders SET fired = TRUE WHERE id = $1", 11,
    )


def test_parse_duration_still_works():
    assert parse_duration("5m") == 300
    assert parse_duration("2h30m") == 9000
    assert parse_duration("") is None
    assert parse_duration("nope") is None


# ── bounded, whole-token durations ───────────────────────────────────────────

@pytest.mark.parametrize("token,seconds", [
    ("30s", 30), ("5m", 300), ("2h30m", 9000), ("1d", 86400), ("1w", 604800),
    ("366d", 366 * 86400),
])
def test_durations_parse(token, seconds):
    assert parse_duration(token) == seconds


@pytest.mark.parametrize("token", [
    "", "nope", "x5my", "5m30", "m5", "5", "0m", "-5m",
    "367d", "100w", "9" * 40 + "w", "99999999999999d",
])
def test_garbage_and_oversized_durations_are_refused(token):
    """Unbounded ones overflowed datetime/timedelta inside /remind, /tldr
    and /shutup: the dispatcher logged a traceback and the user got no
    reply. 'x5my' used to be five minutes."""
    assert parse_duration(token) is None
