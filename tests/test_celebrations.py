"""The daily birthday/anniversary loop: who is due today, and what happens
when the chat can't be posted in."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from ipedro import celebrations
from ipedro.celebrations import _due_today, _feb29_is_today


@pytest.mark.parametrize("day,observed", [
    (date(2026, 2, 28), True),        # no Feb 29 this year: observe it today
    (date(2027, 2, 28), True),
    (date(2028, 2, 28), False),       # leap year: the real Feb 29 is tomorrow
    (date(2028, 2, 29), False),       # ...and matches on its own
    (date(2026, 3, 1), False),
    (date(2026, 1, 28), False),
])
def test_a_leap_day_birthday_is_observed_on_feb_28_in_common_years(day, observed):
    assert _feb29_is_today(day) is observed


@pytest.mark.asyncio
async def test_due_today_passes_the_leap_day_flag_to_the_query():
    db = SimpleNamespace(fetch=AsyncMock(return_value=[]))
    await _due_today(db, date(2027, 2, 28))
    sql, *args = db.fetch.await_args.args
    assert args == [2, 28, date(2027, 2, 28), True]
    assert "cd.month = 2 AND cd.day = 29" in sql

    await _due_today(db, date(2028, 2, 29))
    assert db.fetch.await_args.args[1:] == (2, 29, date(2028, 2, 29), False)


def _loop_env(send_error):
    row = {"id": 5, "chat_id": -100, "user_id": 7, "label": "birthday", "month": 2,
           "day": 29, "year": 2000, "note": None, "name": "Ann"}
    db = SimpleNamespace(
        fetch=AsyncMock(return_value=[row]), execute=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock(
        side_effect=send_error, return_value=SimpleNamespace(message_id=1)))
    settings = SimpleNamespace(tzinfo=None)
    return db, bot, settings


async def _one_pass(db, bot, settings):
    stop = asyncio.Event()
    task = asyncio.create_task(celebrations.run_celebrations_loop(bot, db, settings, stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_a_chat_that_refuses_us_is_stamped_so_it_is_not_retried_all_day():
    err = TelegramForbiddenError(method=None, message="bot was kicked")
    db, bot, settings = _loop_env(err)
    await _one_pass(db, bot, settings)
    assert bot.send_message.await_count == 1
    db.execute.assert_awaited_once()                      # stamped


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_next_tick_not_stamped():
    err = TelegramRetryAfter(method=None, message="slow down", retry_after=3)
    db, bot, settings = _loop_env(err)
    await _one_pass(db, bot, settings)
    db.execute.assert_not_awaited()
