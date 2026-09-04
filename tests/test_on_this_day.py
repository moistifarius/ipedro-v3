"""Tests for the 'On this day' nostalgia feature.

The DB and OpenAI are stubbed; we exercise the pure date/selection/render
helpers plus the build_on_this_day orchestration (which period it picks,
graceful empty handling).
"""

from __future__ import annotations

import random
from datetime import date, datetime, timezone

import pytest

from ipedro.config import Settings
from ipedro.on_this_day import (
    OnThisDayResult,
    _local_day_utc_bounds,
    _months_ago,
    _select_quotes,
    build_on_this_day,
    render_on_this_day,
)


def _settings(tz="America/Los_Angeles"):
    s = Settings(  # type: ignore[call-arg]
        telegram_bot_token="t", openai_api_key="k",
        database_url="postgresql://t/t",
    )
    s.bot_timezone = tz
    return s


# ----------------------------------------------------- date math
def test_months_ago_simple():
    assert _months_ago(date(2026, 6, 21), 1) == date(2026, 5, 21)
    assert _months_ago(date(2026, 6, 21), 3) == date(2026, 3, 21)
    assert _months_ago(date(2026, 6, 21), 12) == date(2025, 6, 21)
    assert _months_ago(date(2026, 6, 21), 24) == date(2024, 6, 21)


def test_months_ago_clamps_short_months():
    # 31 March − 1 month → February, clamped to 28 (2026 not a leap year).
    assert _months_ago(date(2026, 3, 31), 1) == date(2026, 2, 28)
    # Leap year February.
    assert _months_ago(date(2024, 3, 31), 1) == date(2024, 2, 29)
    # 31 May − 1 month → 30 April.
    assert _months_ago(date(2026, 5, 31), 1) == date(2026, 4, 30)


def test_local_day_utc_bounds_span_24h_and_are_utc():
    s = _settings("America/Los_Angeles")
    start, end = _local_day_utc_bounds(date(2026, 6, 21), s.tzinfo)
    assert start.tzinfo == timezone.utc and end.tzinfo == timezone.utc
    assert (end - start).total_seconds() == 24 * 3600
    # PDT is UTC-7 in June, so local midnight is 07:00 UTC.
    assert start.hour == 7


# ----------------------------------------------------- quote selection
def test_select_quotes_dedups_and_spreads_across_people():
    candidates = [
        ("Matt", "the longest most substantive message here"),
        ("Matt", "another matt message"),
        ("Sarah", "sarah said this thing"),
        ("Sarah", "sarah said this thing"),   # exact dup → dropped
        ("Liz", "liz chimes in"),
    ]
    rng = random.Random(0)
    chosen = _select_quotes(candidates, rng)
    assert len(chosen) == 3
    names = [n for n, _ in chosen]
    # Distinct speakers preferred — no duplicate person before everyone's
    # had a turn.
    assert len(set(names)) == 3
    # No exact-duplicate text.
    texts = [t for _, t in chosen]
    assert len(texts) == len(set(texts))


def test_select_quotes_falls_back_to_same_person_when_needed():
    candidates = [
        ("Matt", "first matt line"),
        ("Matt", "second matt line"),
        ("Matt", "third matt line"),
    ]
    chosen = _select_quotes(candidates, random.Random(1))
    assert len(chosen) == 3
    assert all(n == "Matt" for n, _ in chosen)


# ----------------------------------------------------- render
def test_render_includes_quotes_and_header_and_label():
    result = OnThisDayResult(
        label="a year ago today",
        quotes=[("Matt", "we should get tacos"), ("Sarah", "no, sushi")],
        header="Sh-sha. The tape doesn't lie.",
    )
    body = render_on_this_day(result)
    assert "a year ago today" in body
    assert "we should get tacos" in body
    assert "Matt" in body and "Sarah" in body
    assert "Sh-sha. The tape doesn't lie." in body


def test_render_truncates_runaway_quotes():
    long_text = "x" * 400
    body = render_on_this_day(OnThisDayResult(
        label="a month ago today",
        quotes=[("Matt", long_text)], header="",
    ))
    assert "…" in body
    # The quote line shouldn't carry the full 400 chars.
    assert long_text not in body


# ----------------------------------------------------- orchestration
class _FakeOpenAI:
    def __init__(self, line="Sh-sha. Filed."):
        self.line = line

    async def cheap_completion(self, *a, **k):
        return self.line


class _FakeDB:
    """Returns canned rows keyed by which month-offset window is queried.

    We map the UTC start hour-free: the orchestrator walks periods longest
    → shortest, so we answer based on how many calls have happened plus an
    explicit 'hit' set of months.
    """
    def __init__(self, hits: dict[int, list[tuple[str, str]]], today: date, tz):
        self._hits = hits
        self._today = today
        self._tz = tz

    async def fetch(self, query, chat_id, start_utc, end_utc, min_chars):
        # Reverse-engineer which period this window corresponds to by
        # comparing the local target date to today − N months.
        from ipedro.on_this_day import _months_ago, _PERIODS
        local_start = start_utc.astimezone(self._tz).date()
        for months, _label in _PERIODS:
            if _months_ago(self._today, months) == local_start:
                rows = self._hits.get(months, [])
                return [{"name": n, "text": t} for n, t in rows]
        return []


@pytest.mark.asyncio
async def test_build_picks_oldest_period_with_messages():
    s = _settings("UTC")
    today = date(2026, 6, 21)
    # Both 1-month and 12-month windows have data; the OLDEST (12) wins.
    db = _FakeDB(
        hits={
            1: [("Matt", "recent-ish thing from a month ago")],
            12: [("Sarah", "a whole year ago i said this long thing")],
        },
        today=today, tz=s.tzinfo,
    )
    result = await build_on_this_day(
        db, _FakeOpenAI(), s, chat_id=1, today=today,
    )
    assert result is not None
    assert result.label == "a year ago today"
    assert any("year ago" in t for _, t in result.quotes)


@pytest.mark.asyncio
async def test_build_returns_none_when_nothing_found():
    s = _settings("UTC")
    today = date(2026, 6, 21)
    db = _FakeDB(hits={}, today=today, tz=s.tzinfo)
    result = await build_on_this_day(
        db, _FakeOpenAI(), s, chat_id=1, today=today,
    )
    assert result is None


@pytest.mark.asyncio
async def test_build_uses_fallback_header_when_ai_unavailable():
    s = _settings("UTC")
    today = date(2026, 6, 21)
    db = _FakeDB(
        hits={1: [("Matt", "something substantive from back then")]},
        today=today, tz=s.tzinfo,
    )

    class _DeadAI:
        async def cheap_completion(self, *a, **k):
            return None

    result = await build_on_this_day(db, _DeadAI(), s, chat_id=1, today=today)
    assert result is not None
    # A non-empty fallback header still ships.
    assert result.header.strip()
