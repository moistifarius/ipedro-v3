"""Month-in-review recap: bounds, highlight selection, rendering, build, loop."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import monthly_recap as mr


def _settings():
    return SimpleNamespace(tzinfo=timezone.utc)


@pytest.mark.parametrize("today,label", [
    (date(2026, 8, 2), "July 2026"),
    (date(2026, 1, 15), "December 2025"),   # year rollover
    (date(2026, 3, 31), "February 2026"),
])
def test_prev_month_label(today, label):
    assert mr._prev_month_bounds(today, timezone.utc)[0] == label


def test_prev_month_utc_bounds():
    label, prev_first, cur_first, start_utc, end_utc = \
        mr._prev_month_bounds(date(2026, 8, 2), timezone.utc)
    assert prev_first == date(2026, 7, 1) and cur_first == date(2026, 8, 1)
    assert start_utc.year == 2026 and start_utc.month == 7 and start_utc.day == 1
    assert end_utc.month == 8 and end_utc.day == 1


def test_highlights_prefer_saved_quotes_then_spread_people():
    saved = [("Matt", "saved gold"), ("Matt", "another matt quote")]
    longs = [("Luke", "a nice long message"), ("Matt", "saved gold"),  # dup text
             ("Ann", "ann was here too")]
    picks = mr._select_highlights(saved, longs)
    # Saved quote comes first; duplicate text dropped; different people spread
    # before Matt repeats.
    assert picks[0] == ("Matt", "saved gold")
    names_in_order = [n for n, _ in picks]
    assert names_in_order.index("Luke") < names_in_order.count("Matt") + 3  # Luke/Ann appear
    assert ("Matt", "saved gold") in picks
    # No duplicate texts.
    texts = [t for _, t in picks]
    assert len(texts) == len(set(texts))


def test_render_contains_all_sections():
    result = mr.MonthlyRecapResult(
        month_label="July 2026",
        recap="What a month, mostly Matt's fault.",
        highlights=[("Matt", "the funniest thing"), ("Luke", "the second thing")],
        stats=mr.RecapStats(messages=812, people=5, top_name="Matt",
                            top_count=311, quotes_saved=9),
    )
    body = mr.render_monthly_recap(result)
    assert "July 2026 in review" in body
    assert "What a month" in body
    assert "the funniest thing" in body and "— Matt" in body
    assert "812 messages" in body and "5 people" in body
    assert "top yapper: Matt (311)" in body and "9 quotes saved" in body


class _RecapFakeDB:
    """Answers the specific queries build_monthly_recap / the loop issue."""

    def __init__(self, *, stats_rows, quotes_count, saved, longs, pool,
                 eligible=(100,)):
        self._stats = stats_rows
        self._quotes_count = quotes_count
        self._saved = saved
        self._longs = longs
        self._pool = pool
        self._eligible = list(eligible)
        self.stamped: list = []

    async def fetch(self, query, *args):
        if "GROUP BY m.user_id" in query:
            return self._stats
        if "SELECT quoted_name AS name" in query:
            return [{"name": n, "text": t} for n, t in self._saved]
        if "ORDER BY char_length(m.content) DESC" in query:
            return [{"name": n, "text": t} for n, t in self._longs]
        if "ORDER BY m.created_at ASC" in query:
            return [{"name": n, "text": t} for n, t in self._pool]
        if "monthly_recap_enabled" in query:
            return [{"chat_id": c} for c in self._eligible]
        return []

    async def fetchval(self, query, *args):
        if "COUNT(*) FROM quotes" in query:
            return self._quotes_count
        return 0

    async def execute(self, query, *args):
        if "last_monthly_recap" in query:
            self.stamped.append(args)
        return "OK"


def _openai(recap="what a month"):
    return SimpleNamespace(cheap_completion=AsyncMock(return_value=recap))


@pytest.mark.asyncio
async def test_build_recap_full():
    db = _RecapFakeDB(
        stats_rows=[{"name": "Matt", "n": 10}, {"name": "Luke", "n": 4}],
        quotes_count=3,
        saved=[("Matt", "the saved one")],
        longs=[("Luke", "a substantive message from luke")],
        pool=[("Matt", "hi"), ("Luke", "yo")],
    )
    res = await mr.build_monthly_recap(db, _openai(), _settings(), 100,
                                       today=date(2026, 8, 2))
    assert res is not None
    assert res.month_label == "July 2026"
    assert res.recap == "what a month"
    assert res.stats.messages == 14 and res.stats.people == 2
    assert res.stats.top_name == "Matt" and res.stats.top_count == 10
    assert res.stats.quotes_saved == 3
    assert ("Matt", "the saved one") in res.highlights


@pytest.mark.asyncio
async def test_build_recap_none_for_empty_month():
    db = _RecapFakeDB(stats_rows=[], quotes_count=0, saved=[], longs=[], pool=[])
    res = await mr.build_monthly_recap(db, _openai(), _settings(), 100,
                                       today=date(2026, 8, 2))
    assert res is None


@pytest.mark.asyncio
async def test_ai_recap_falls_back_when_model_silent():
    db = _RecapFakeDB(stats_rows=[{"name": "Matt", "n": 5}], quotes_count=0,
                      saved=[], longs=[("Matt", "something long enough here")],
                      pool=[("Matt", "hello there")])
    res = await mr.build_monthly_recap(db, _openai(recap=None), _settings(), 100,
                                       today=date(2026, 8, 2))
    assert res is not None and res.recap == mr._FALLBACK_RECAP


@pytest.mark.asyncio
async def test_loop_posts_and_stamps():
    db = _RecapFakeDB(
        stats_rows=[{"name": "Matt", "n": 10}], quotes_count=1,
        saved=[("Matt", "quote")], longs=[("Matt", "long message here yep")],
        pool=[("Matt", "hi")], eligible=(100,),
    )
    bot = SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=7)))
    settings = _settings()
    await mr._maybe_post(bot, db, _openai(), settings,
                         now=datetime(2026, 8, 2, 10, 0, tzinfo=settings.tzinfo))
    bot.send_message.assert_awaited_once()
    body = bot.send_message.await_args.args[1]
    assert "in review" in body
    assert db.stamped                      # chat was stamped so it won't repeat


@pytest.mark.asyncio
async def test_loop_waits_for_a_civilised_hour():
    """No recap at midnight the moment the month rolls over."""
    db = _RecapFakeDB(
        stats_rows=[{"name": "Matt", "n": 10}], quotes_count=1,
        saved=[("Matt", "quote")], longs=[("Matt", "long message here yep")],
        pool=[("Matt", "hi")], eligible=(100,),
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    settings = _settings()
    await mr._maybe_post(bot, db, _openai(), settings,
                         now=datetime(2026, 8, 1, 0, 30, tzinfo=settings.tzinfo))
    bot.send_message.assert_not_awaited()
    assert not db.stamped


@pytest.mark.asyncio
async def test_loop_stamps_on_permanent_send_failure():
    """A chat the bot was kicked from must stop costing an AI call hourly."""
    from aiogram.exceptions import TelegramForbiddenError

    db = _RecapFakeDB(
        stats_rows=[{"name": "Matt", "n": 10}], quotes_count=1,
        saved=[("Matt", "quote")], longs=[("Matt", "long message here yep")],
        pool=[("Matt", "hi")], eligible=(100,),
    )
    bot = SimpleNamespace(send_message=AsyncMock(
        side_effect=TelegramForbiddenError(
            method=SimpleNamespace(), message="Forbidden: bot was kicked")))
    settings = _settings()
    await mr._maybe_post(bot, db, _openai(), settings,
                         now=datetime(2026, 8, 2, 10, 0, tzinfo=settings.tzinfo))
    assert db.stamped            # stamped despite the failed send


@pytest.mark.asyncio
async def test_loop_does_not_stamp_on_transient_send_failure():
    db = _RecapFakeDB(
        stats_rows=[{"name": "Matt", "n": 10}], quotes_count=1,
        saved=[("Matt", "quote")], longs=[("Matt", "long message here yep")],
        pool=[("Matt", "hi")], eligible=(100,),
    )
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("blip")))
    settings = _settings()
    await mr._maybe_post(bot, db, _openai(), settings,
                         now=datetime(2026, 8, 2, 10, 0, tzinfo=settings.tzinfo))
    assert not db.stamped        # retries next tick
