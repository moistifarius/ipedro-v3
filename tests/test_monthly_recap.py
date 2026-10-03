"""Month-in-review recap: bounds, what gets quoted (rarely), rendering,
build, loop."""

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


def test_render_quotes_only_what_was_picked_with_no_header():
    result = mr.MonthlyRecapResult(
        month_label="July 2026",
        recap="What a month, mostly Matt's fault.",
        quotes=[("Matt", "the funniest thing")],
        stats=mr.RecapStats(messages=812, people=5, top_name="Matt",
                            top_count=311, quotes_saved=9),
    )
    body = mr.render_monthly_recap(result)
    assert "July 2026 in review" in body
    assert "What a month" in body
    assert "“the funniest thing” — Matt" in body
    assert "Highlights" not in body
    assert "812 messages" in body and "5 people" in body
    assert "top yapper: Matt (311)" in body and "9 quotes saved" in body


def test_render_with_no_quotes_is_just_the_recap_and_stats():
    result = mr.MonthlyRecapResult(
        month_label="July 2026", recap="Quiet one.", quotes=[],
        stats=mr.RecapStats(messages=40, people=3, top_name="Ann",
                            top_count=20, quotes_saved=0),
    )
    assert mr.render_monthly_recap(result) == (
        "🗓️ July 2026 in review\n\nQuiet one.\n\n"
        "📊 40 messages · 3 people · top yapper: Ann (20)"
    )


class _RecapFakeDB:
    """Answers the specific queries build_monthly_recap / the loop issue."""

    def __init__(self, *, stats_rows, quotes_count, saved, pool,
                 eligible=(100,), persona=("dude", None)):
        self._stats = stats_rows
        self._quotes_count = quotes_count
        self._saved = saved
        self._pool = pool
        self._eligible = list(eligible)
        self._persona = persona
        self.stamped: list = []

    async def fetch(self, query, *args):
        if "GROUP BY m.user_id" in query:
            return self._stats
        if "SELECT quoted_name AS name" in query:
            return [{"name": n, "text": t} for n, t in self._saved]
        if "WITH month AS" in query:
            return [{"name": n, "text": t} for n, t in self._pool]
        if "monthly_recap_enabled" in query:
            return [{"chat_id": c} for c in self._eligible]
        return []

    async def fetchrow(self, query, *args):
        if "FROM chat_config" in query and self._persona is not None:
            return {"persona": self._persona[0], "persona_custom": self._persona[1]}
        return None

    async def fetchval(self, query, *args):
        if "COUNT(*) FROM quotes" in query:
            return self._quotes_count
        return 0

    async def execute(self, query, *args):
        if "last_monthly_recap" in query:
            self.stamped.append(args)
        return "OK"


def _openai(reply="RECAP: what a month\nQUOTES: NONE"):
    return SimpleNamespace(chat=AsyncMock(return_value=reply))


def _db(**over):
    base = dict(
        stats_rows=[{"name": "Matt", "n": 10}, {"name": "Luke", "n": 4}],
        quotes_count=3,
        saved=[("Matt", "the saved one")],
        pool=[("Matt", "hello there everyone"), ("Luke", "propane is the future")],
    )
    base.update(over)
    return _RecapFakeDB(**base)


async def _build(db, openai):
    return await mr.build_monthly_recap(db, openai, _settings(), 100,
                                        today=date(2026, 8, 2))


@pytest.mark.asyncio
async def test_build_recap_full():
    res = await _build(_db(), _openai("RECAP: what a month\nQUOTES: 1"))
    assert res is not None
    assert res.month_label == "July 2026"
    assert res.recap == "what a month"
    assert res.quotes == [("Matt", "the saved one")]
    assert res.stats.messages == 14 and res.stats.people == 2
    assert res.stats.top_name == "Matt" and res.stats.top_count == 10
    assert res.stats.quotes_saved == 3


@pytest.mark.asyncio
async def test_none_means_no_quotes_at_all():
    """The old recap always posted six 'highlights'. Now nothing gets
    quoted unless the model picked it."""
    res = await _build(_db(), _openai("RECAP: slow month.\nQUOTES: NONE"))
    assert res.quotes == []
    assert "“" not in mr.render_monthly_recap(res)


@pytest.mark.asyncio
async def test_quotes_are_ours_verbatim_by_number_never_the_models_words():
    """The model names numbers; the text and the name come from our copy,
    so a quote can't be paraphrased or pinned on the wrong person."""
    res = await _build(_db(), _openai(
        "RECAP: Luke went full propane.\nQUOTES: 3 (the propane one)"
    ))
    assert res.quotes == [("Luke", "propane is the future")]


@pytest.mark.asyncio
async def test_at_most_two_quotes_and_bad_numbers_ignored():
    pool = [("Ann", f"line number {i} here") for i in range(1, 6)]
    res = await _build(_db(saved=[], pool=pool),
                       _openai("RECAP: busy.\nQUOTES: 9, 2, 2, 0, 4, 5"))
    assert res.quotes == [("Ann", "line number 2 here"), ("Ann", "line number 4 here")]


@pytest.mark.asyncio
async def test_an_overlong_pick_is_dropped_not_cut_short():
    long_line = "word " * 80
    res = await _build(_db(saved=[], pool=[("Ann", long_line.strip())]),
                       _openai("RECAP: hm.\nQUOTES: 1"))
    assert res.quotes == []


@pytest.mark.asyncio
async def test_a_reply_without_the_format_is_all_recap_and_no_quotes():
    res = await _build(_db(), _openai("Sh-sha. Big month. Matt did a thing."))
    assert res.recap == "Sh-sha. Big month. Matt did a thing."
    assert res.quotes == []


@pytest.mark.asyncio
async def test_build_recap_none_for_empty_month():
    db = _RecapFakeDB(stats_rows=[], quotes_count=0, saved=[], pool=[])
    assert await _build(db, _openai()) is None


@pytest.mark.asyncio
async def test_model_down_falls_back_and_never_dumps_quotes():
    res = await _build(_db(), _openai(None))
    assert res.recap == mr._FALLBACK_RECAP
    assert res.quotes == []


@pytest.mark.asyncio
async def test_written_in_the_chats_own_persona():
    openai = _openai()
    await _build(_db(persona=("dude", "You are the soup chat's Dale.")), openai)
    system = openai.chat.await_args.args[0][0]
    assert system == {"role": "system", "content": "You are the soup chat's Dale."}


@pytest.mark.asyncio
async def test_the_model_sees_numbered_lines_saved_ones_marked_once_each():
    openai = _openai()
    await _build(_db(pool=[("Matt", "the saved one"), ("Luke", "multi\nline   msg here")]),
                 openai)
    prompt = openai.chat.await_args.args[0][1]["content"]
    assert "1. [saved] Matt: the saved one" in prompt
    assert "2. Luke: multi line msg here" in prompt
    assert prompt.count("the saved one") == 1          # deduped against the pool
    assert "at most 2" in prompt and "NONE" in prompt


def test_parse_reply():
    assert mr._parse_reply("RECAP: a\nb\nQUOTES: 2", 3) == ("a\nb", [2])
    assert mr._parse_reply("recap: x\nquotes: none", 3) == ("x", [])
    assert mr._parse_reply(None, 3) == ("", [])


@pytest.mark.asyncio
async def test_loop_posts_and_stamps():
    db = _db()
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
    db = _db()
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

    db = _db()
    bot = SimpleNamespace(send_message=AsyncMock(
        side_effect=TelegramForbiddenError(
            method=SimpleNamespace(), message="Forbidden: bot was kicked")))
    settings = _settings()
    await mr._maybe_post(bot, db, _openai(), settings,
                         now=datetime(2026, 8, 2, 10, 0, tzinfo=settings.tzinfo))
    assert db.stamped            # stamped despite the failed send


@pytest.mark.asyncio
async def test_loop_does_not_stamp_on_transient_send_failure():
    db = _db()
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("blip")))
    settings = _settings()
    await mr._maybe_post(bot, db, _openai(), settings,
                         now=datetime(2026, 8, 2, 10, 0, tzinfo=settings.tzinfo))
    assert not db.stamped        # retries next tick
