"""One chat's failure must not skip every OTHER due chat in the same tick.

Regression: comic.py's per-tick body, and ambient_loops.py's yearly-retro
and daily-fortune loops, each iterated their due chats with no per-chat
try/except — a DB hiccup or send failure partway through the list raised
out of the whole tick, silently skipping every chat still queued behind
the one that failed (it would retry next tick, but that's minutes-to-an-
hour of a working feature going dark for chats that did nothing wrong).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import ambient_loops
from ipedro.comic import run_comic_loop


# ── comic.py ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_comic_loop_keeps_going_after_one_chat_fails(monkeypatch):
    monkeypatch.setattr(
        "ipedro.comic._chats_due", AsyncMock(return_value=[1, 2, 3]),
    )

    calls = []

    async def fake_build_and_post(chat_id, bot, db, openai):
        calls.append(chat_id)
        if chat_id == 2:
            raise RuntimeError("boom")
        return "posted"

    monkeypatch.setattr("ipedro.comic._build_and_post", fake_build_and_post)
    db = SimpleNamespace(execute=AsyncMock())
    stop = asyncio.Event()

    async def stop_after_first_tick(coro, *a, **k):
        coro.close()          # never actually awaited; avoid the warning
        stop.set()
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", stop_after_first_tick)

    await run_comic_loop(bot=SimpleNamespace(), db=db, openai=SimpleNamespace(), stop=stop)

    assert calls == [1, 2, 3]                 # chat 3 was still attempted
    # Every chat is stamped so none is retried next tick: the two successes
    # as done, the failure (chat 2) as due again in a few hours.
    assert db.execute.await_count == 3


# ── ambient_loops.py: yearly retro ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_yearly_retro_keeps_going_after_one_chat_fails(monkeypatch):
    settings = SimpleNamespace(tzinfo=__import__("datetime").timezone.utc)
    db = SimpleNamespace(
        fetch=AsyncMock(return_value=[
            {"chat_id": 1}, {"chat_id": 2}, {"chat_id": 3},
        ]),
    )
    calls = []

    async def fake_per_chat(bot, db, openai, chat_id, year):
        calls.append(chat_id)
        if chat_id == 2:
            raise RuntimeError("db hiccup")

    monkeypatch.setattr(
        ambient_loops, "_maybe_yearly_retro_for_chat", fake_per_chat,
    )
    # Force the Dec-31 gate open regardless of the real date.
    monkeypatch.setattr(
        ambient_loops, "datetime",
        SimpleNamespace(now=lambda tz: __import__("datetime").datetime(
            2026, 12, 31, tzinfo=tz,
        )),
    )

    await ambient_loops._maybe_yearly_retro(
        bot=SimpleNamespace(), db=db, openai=SimpleNamespace(), settings=settings,
    )
    assert calls == [1, 2, 3]                 # chat 3 was still attempted


# ── ambient_loops.py: daily fortune ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_daily_fortune_keeps_going_after_one_chat_fails(monkeypatch):
    db = SimpleNamespace(
        fetch=AsyncMock(return_value=[
            {"chat_id": 1}, {"chat_id": 2}, {"chat_id": 3},
        ]),
    )
    calls = []

    async def fake_per_chat(bot, db, openai, chat_id, today):
        calls.append(chat_id)
        if chat_id == 2:
            raise RuntimeError("send exploded")

    monkeypatch.setattr(
        ambient_loops, "_maybe_daily_fortune_for_chat", fake_per_chat,
    )

    await ambient_loops._maybe_daily_fortune(
        bot=SimpleNamespace(), db=db, openai=SimpleNamespace(),
        settings=SimpleNamespace(tzinfo=__import__("datetime").timezone.utc),
    )
    assert calls == [1, 2, 3]                 # chat 3 was still attempted


# ── confessions go to groups, never into somebody's DM ──────────────────────

@pytest.mark.asyncio
async def test_a_confession_is_only_ever_surfaced_into_a_group(monkeypatch):
    """/confess is a DM command, so a private chat is one person. The
    surfacer drew from every chat seen lately, DMs included, and marked the
    confession surfaced: it landed in one stranger's DM and never reached a
    group."""
    from ipedro import ambient_loops as al

    monkeypatch.setattr(al.random, "random", lambda: 0.0)
    seen = []

    async def fetchrow(query, *a):
        return {"id": 1, "text": "i ate the last slice"}

    async def fetch(query, *a):
        seen.append(query)
        return [{"chat_id": -100}]

    db = SimpleNamespace(fetchrow=fetchrow, fetch=fetch, execute=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=5)))
    await al._maybe_surface_confession(bot, db)
    assert "type IN ('group', 'supergroup')" in seen[0]
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == -100
