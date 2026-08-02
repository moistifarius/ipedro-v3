"""Month-in-review — a best-of recap posted at the start of each month.

Replaces the daily 'on this day' auto-post (that module still powers the
on-demand /onthisday command). Once per opted-in, recently-active chat, when a
new local month begins, the bot posts a recap of the month just finished:

  * an in-character AI 'month in review' line or two,
  * a handful of verbatim highlights (saved quotes first — those are the bits
    people already flagged as good — then the month's meatiest messages),
  * a compact stats line (messages, people, top yapper, quotes saved).

Every message fed to the AI is labelled with the speaker's name so the recap
attributes things to the right person. Restart-safe via
chat_state.last_monthly_recap. Degrades gracefully when the AI is down.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from aiogram import Bot

from ipedro.bot_messages import track
from ipedro.config import Settings
from ipedro.db.pool import Database
from ipedro.openai_client import OpenAIClient
from ipedro.prompts import MONTHLY_RECAP_PROMPT
from ipedro.silenced_chats import is_silenced

log = logging.getLogger(__name__)

_TICK_SECONDS = 3600  # hourly, like the other daily loops
_ACTIVE_WINDOW_DAYS = 45
_MIN_MESSAGE_CHARS = 12
_MAX_HIGHLIGHTS = 6
_AI_POOL_SIZE = 60           # messages sampled for the AI recap
_MAX_QUOTE_CHARS = 280

# name of the message author, best-effort, from the users join.
_NAME_SQL = (
    "COALESCE(NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), ''), "
    "u.username, 'someone')"
)


@dataclass(frozen=True)
class RecapStats:
    messages: int
    people: int
    top_name: str | None
    top_count: int
    quotes_saved: int


@dataclass(frozen=True)
class MonthlyRecapResult:
    month_label: str                    # "July 2026"
    recap: str                          # the AI (or fallback) review
    highlights: list[tuple[str, str]]   # (name, text) verbatim
    stats: RecapStats | None = field(default=None)


def _prev_month_bounds(today: date, tz):
    """(label, prev_first_local, cur_first_local, start_utc, end_utc) for the
    calendar month immediately before the one containing ``today``."""
    cur_first = date(today.year, today.month, 1)
    if today.month == 1:
        prev_first = date(today.year - 1, 12, 1)
    else:
        prev_first = date(today.year, today.month - 1, 1)
    start_utc = datetime.combine(prev_first, time.min, tzinfo=tz).astimezone(timezone.utc)
    end_utc = datetime.combine(cur_first, time.min, tzinfo=tz).astimezone(timezone.utc)
    return prev_first.strftime("%B %Y"), prev_first, cur_first, start_utc, end_utc


async def _fetch_stats(db: Database, chat_id: int, start_utc, end_utc) -> RecapStats:
    rows = await db.fetch(
        f"""
        SELECT {_NAME_SQL} AS name, COUNT(*) AS n
          FROM messages m
          LEFT JOIN users u ON u.user_id = m.user_id
         WHERE m.chat_id = $1 AND m.role = 'user'
           AND m.created_at >= $2 AND m.created_at < $3
         GROUP BY u.user_id, u.first_name, u.last_name, u.username
         ORDER BY n DESC
        """,
        chat_id, start_utc, end_utc,
    )
    quotes_saved = await db.fetchval(
        "SELECT COUNT(*) FROM quotes "
        " WHERE chat_id = $1 AND created_at >= $2 AND created_at < $3",
        chat_id, start_utc, end_utc,
    )
    total = sum(r["n"] for r in rows)
    top = rows[0] if rows else None
    return RecapStats(
        messages=total, people=len(rows),
        top_name=top["name"] if top else None,
        top_count=top["n"] if top else 0,
        quotes_saved=int(quotes_saved or 0),
    )


async def _fetch_saved_quotes(db: Database, chat_id: int, start_utc, end_utc):
    rows = await db.fetch(
        "SELECT quoted_name AS name, text FROM quotes "
        " WHERE chat_id = $1 AND created_at >= $2 AND created_at < $3 "
        " ORDER BY id DESC LIMIT 20",
        chat_id, start_utc, end_utc,
    )
    return [(r["name"] or "someone", r["text"].strip()) for r in rows if r["text"]]


async def _fetch_long_messages(db: Database, chat_id: int, start_utc, end_utc):
    rows = await db.fetch(
        f"""
        SELECT {_NAME_SQL} AS name, m.content AS text
          FROM messages m
          LEFT JOIN users u ON u.user_id = m.user_id
         WHERE m.chat_id = $1 AND m.role = 'user'
           AND m.created_at >= $2 AND m.created_at < $3
           AND char_length(TRIM(m.content)) >= $4
           AND LEFT(TRIM(m.content), 1) <> '/'
         ORDER BY char_length(m.content) DESC
         LIMIT 25
        """,
        chat_id, start_utc, end_utc, _MIN_MESSAGE_CHARS,
    )
    return [(r["name"], r["text"].strip()) for r in rows]


async def _fetch_recap_pool(db: Database, chat_id: int, start_utc, end_utc):
    """A chronological sample of substantive messages (name, text) for the AI
    to narrate. Sampled evenly across the month so the recap isn't front- or
    back-loaded."""
    rows = await db.fetch(
        f"""
        SELECT {_NAME_SQL} AS name, m.content AS text
          FROM messages m
          LEFT JOIN users u ON u.user_id = m.user_id
         WHERE m.chat_id = $1 AND m.role = 'user'
           AND m.created_at >= $2 AND m.created_at < $3
           AND char_length(TRIM(m.content)) >= $4
           AND LEFT(TRIM(m.content), 1) <> '/'
         ORDER BY m.created_at ASC
         LIMIT 400
        """,
        chat_id, start_utc, end_utc, _MIN_MESSAGE_CHARS,
    )
    pool = [(r["name"], r["text"].strip()) for r in rows]
    if len(pool) <= _AI_POOL_SIZE:
        return pool
    stride = len(pool) / _AI_POOL_SIZE
    return [pool[int(i * stride)] for i in range(_AI_POOL_SIZE)]


def _select_highlights(
    saved: list[tuple[str, str]], long_msgs: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Saved quotes first (already curated), then the meatiest messages.
    Dedups identical text and spreads across people before repeating one."""
    seen_text: set[str] = set()
    seen_people: set[str] = set()
    primary: list[tuple[str, str]] = []
    extras: list[tuple[str, str]] = []
    for name, text in list(saved) + list(long_msgs):
        key = text.lower()
        if key in seen_text:
            continue
        seen_text.add(key)
        if name in seen_people:
            extras.append((name, text))
        else:
            seen_people.add(name)
            primary.append((name, text))
    chosen = primary[:_MAX_HIGHLIGHTS]
    if len(chosen) < _MAX_HIGHLIGHTS:
        chosen += extras[: _MAX_HIGHLIGHTS - len(chosen)]
    return chosen


_FALLBACK_RECAP = "Another month in the books. Here's what stuck to the tape."


async def _ai_recap(
    openai: OpenAIClient, month_label: str,
    pool: list[tuple[str, str]], chat_id: int,
) -> str:
    if not pool:
        return _FALLBACK_RECAP
    joined = "\n".join(f"- {name}: {text}" for name, text in pool)
    line = await openai.cheap_completion(
        MONTHLY_RECAP_PROMPT.format(month=month_label, messages=joined[:4000]),
        max_tokens=220, chat_id=chat_id,
    )
    return (line or "").strip() or _FALLBACK_RECAP


async def build_monthly_recap(
    db: Database, openai: OpenAIClient, settings: Settings, chat_id: int,
    *, today: date | None = None,
) -> MonthlyRecapResult | None:
    """Recap the calendar month before ``today``. None if the month was empty."""
    tz = settings.tzinfo
    today = today or datetime.now(tz).date()
    month_label, _prev_first, _cur_first, start_utc, end_utc = _prev_month_bounds(today, tz)

    stats = await _fetch_stats(db, chat_id, start_utc, end_utc)
    if stats.messages == 0:
        return None

    saved = await _fetch_saved_quotes(db, chat_id, start_utc, end_utc)
    long_msgs = await _fetch_long_messages(db, chat_id, start_utc, end_utc)
    highlights = _select_highlights(saved, long_msgs)
    pool = await _fetch_recap_pool(db, chat_id, start_utc, end_utc)
    recap = await _ai_recap(openai, month_label, pool, chat_id)

    return MonthlyRecapResult(
        month_label=month_label, recap=recap, highlights=highlights, stats=stats,
    )


def render_monthly_recap(result: MonthlyRecapResult) -> str:
    lines = [f"🗓️ {result.month_label} in review", "", result.recap]
    if result.highlights:
        lines.append("")
        lines.append("Highlights:")
        for name, text in result.highlights:
            snippet = text if len(text) <= _MAX_QUOTE_CHARS \
                else text[: _MAX_QUOTE_CHARS - 1].rstrip() + "…"
            lines.append(f"“{snippet}” — {name}")
    s = result.stats
    if s and s.messages:
        bits = [f"{s.messages} messages", f"{s.people} people"]
        if s.top_name:
            bits.append(f"top yapper: {s.top_name} ({s.top_count})")
        if s.quotes_saved:
            bits.append(f"{s.quotes_saved} quotes saved")
        lines.append("")
        lines.append("📊 " + " · ".join(bits))
    return "\n".join(lines)


async def _eligible_chats(db: Database, prev_first: date) -> list[int]:
    """Opted-in, recently-active chats that haven't been recapped for the
    month starting ``prev_first`` yet."""
    rows = await db.fetch(
        f"""
        SELECT c.chat_id
          FROM chats c
          JOIN chat_config cfg ON cfg.chat_id = c.chat_id
          LEFT JOIN chat_state cs ON cs.chat_id = c.chat_id
         WHERE cfg.monthly_recap_enabled = TRUE
           AND c.last_seen >= NOW() - INTERVAL '{_ACTIVE_WINDOW_DAYS} days'
           AND (cs.last_monthly_recap IS NULL OR cs.last_monthly_recap < $1)
        """,
        prev_first,
    )
    return [r["chat_id"] for r in rows]


async def _stamp(db: Database, chat_id: int, prev_first: date) -> None:
    await db.execute(
        "INSERT INTO chat_state (chat_id, last_monthly_recap) VALUES ($1, $2) "
        "ON CONFLICT (chat_id) DO UPDATE SET last_monthly_recap = EXCLUDED.last_monthly_recap",
        chat_id, prev_first,
    )


async def _maybe_post(
    bot: Bot, db: Database, openai: OpenAIClient, settings: Settings,
) -> None:
    today = datetime.now(settings.tzinfo).date()
    _label, prev_first, _cur_first, _s, _e = _prev_month_bounds(today, settings.tzinfo)
    for chat_id in await _eligible_chats(db, prev_first):
        try:
            result = await build_monthly_recap(db, openai, settings, chat_id, today=today)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("monthly-recap build failed for %s: %s", chat_id, exc)
            continue
        if result is None:
            await _stamp(db, chat_id, prev_first)   # quiet month → don't re-query
            continue
        text = render_monthly_recap(result)
        try:
            sent = await bot.send_message(
                chat_id, text, disable_notification=is_silenced(chat_id),
            )
            track(chat_id, sent.message_id, text)
            await _stamp(db, chat_id, prev_first)
            log.info("monthly recap posted in chat %s (%s).", chat_id, result.month_label)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("monthly-recap send failed for %s: %s", chat_id, exc)


async def run_monthly_recap_loop(
    bot: Bot, db: Database, openai: OpenAIClient, settings: Settings,
    stop: asyncio.Event,
) -> None:
    """Loop until ``stop`` is set."""
    log.info("Monthly-recap loop running.")
    while not stop.is_set():
        try:
            await _maybe_post(bot, db, openai, settings)
        except Exception as exc:
            log.exception("Monthly-recap iteration failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
    log.info("Monthly-recap loop stopped.")
