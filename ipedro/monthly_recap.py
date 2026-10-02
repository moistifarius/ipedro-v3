"""Month-in-review — a short look back posted at the start of each month.

Replaces the daily 'on this day' auto-post (that module still powers the
on-demand /onthisday command). Once per opted-in, recently-active chat, when a
new local month begins, the bot posts a recap of the month just finished:

  * two or three sentences in the chat's own persona about what people would
    actually remember,
  * at most two verbatim quotes — and only when the model judges one genuinely
    funny or memorable; most months get one or none,
  * a compact stats line (messages, people, top yapper, quotes saved).

It used to append six "highlights" chosen by length — the month's longest
messages — which read as random lines nobody cared about. Quotes are now the
model's pick from a numbered list, rendered from our own copy by number, so
they're always verbatim and always attributed to whoever really said them.

Restart-safe via chat_state.last_monthly_recap. Degrades gracefully when the
AI is down: the fallback line and the stats, never a quote dump.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from ipedro.bot_messages import track
from ipedro.config import Settings
from ipedro.db.pool import Database
from ipedro.openai_client import OpenAIClient
from ipedro.personas import current_master_prompt, resolve_persona
from ipedro.prompts import MONTHLY_RECAP_PROMPT
from ipedro.silenced_chats import is_silenced

log = logging.getLogger(__name__)

_TICK_SECONDS = 3600  # hourly, like the other daily loops
_ACTIVE_WINDOW_DAYS = 45
_MIN_MESSAGE_CHARS = 12
_POOL_SIZE = 150             # messages sampled, evenly across the month
_POOL_LINE_CHARS = 300       # each one cut to this for the model to read
_MAX_QUOTES = 2
# Longer than this isn't a quote, it's a paragraph — and cutting it short
# would cut the punchline, so an over-long pick is dropped, never trimmed.
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
    quotes: list[tuple[str, str]]       # (name, text) verbatim; usually 0-1
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
         GROUP BY m.user_id, u.first_name, u.last_name, u.username
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


async def _fetch_recap_pool(db: Database, chat_id: int, start_utc, end_utc):
    """Up to _POOL_SIZE substantive messages (name, text), spread evenly over
    the WHOLE month. (It used to take the month's first 400 and sample those,
    so in a busy chat the recap only ever saw the first few days.) Commands
    and bare media notes ("[photo: …]") are left out: neither is something
    anyone said."""
    rows = await db.fetch(
        f"""
        WITH month AS (
            SELECT {_NAME_SQL} AS name, m.content AS text, m.created_at,
                   ROW_NUMBER() OVER (ORDER BY m.created_at) AS rn,
                   COUNT(*) OVER () AS total
              FROM messages m
              LEFT JOIN users u ON u.user_id = m.user_id
             WHERE m.chat_id = $1 AND m.role = 'user'
               AND m.created_at >= $2 AND m.created_at < $3
               AND char_length(TRIM(m.content)) >= $4
               AND LEFT(TRIM(m.content), 1) NOT IN ('/', '[')
        )
        SELECT name, text FROM month
         WHERE (rn - 1) % GREATEST(CEIL(total::numeric / $5::int)::int, 1) = 0
         ORDER BY created_at
         LIMIT $5::int
        """,
        chat_id, start_utc, end_utc, _MIN_MESSAGE_CHARS, _POOL_SIZE,
    )
    return [(r["name"], r["text"].strip()) for r in rows]


def _candidates(
    saved: list[tuple[str, str]], pool: list[tuple[str, str]],
) -> list[tuple[str, str, bool]]:
    """(name, text, saved) for the model to read and pick from: lines
    someone saved with /quote first, then the month's sample, each text
    once."""
    seen: set[str] = set()
    out: list[tuple[str, str, bool]] = []
    for (name, text), was_saved in (
        [(q, True) for q in saved] + [(m, False) for m in pool]
    ):
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append((name, text, was_saved))
    return out


_FALLBACK_RECAP = "Another month in the books."

_QUOTES_LINE_RE = re.compile(r"^\s*QUOTES\s*:(.*)$", re.IGNORECASE | re.MULTILINE)
_RECAP_LABEL_RE = re.compile(r"^\s*RECAP\s*:\s*", re.IGNORECASE)


def _parse_reply(raw: str | None, n_candidates: int) -> tuple[str, list[int]]:
    """The model's 'RECAP: … / QUOTES: 3, 17 | NONE' reply as (recap, the
    1-based candidate numbers it picked). Anything unparseable picks
    nothing: no quote is always the safe answer."""
    text = (raw or "").strip()
    picks: list[int] = []
    m = _QUOTES_LINE_RE.search(text)
    if m:
        for num in re.findall(r"\d+", m.group(1)):
            i = int(num)
            if 1 <= i <= n_candidates and i not in picks:
                picks.append(i)
        text = text[: m.start()].strip()
    return _RECAP_LABEL_RE.sub("", text, count=1).strip(), picks[:_MAX_QUOTES]


async def _chat_persona(db: Database, chat_id: int) -> str:
    try:
        row = await db.fetchrow(
            "SELECT persona, persona_custom FROM chat_config WHERE chat_id = $1",
            chat_id,
        )
    except Exception as exc:                       # pragma: no cover - defensive
        log.info("monthly-recap: persona lookup failed for %s: %s", chat_id, exc)
        row = None
    if row is None:
        return current_master_prompt()
    return resolve_persona(row["persona"], row["persona_custom"])


async def _ai_recap(
    openai: OpenAIClient, month_label: str,
    candidates: list[tuple[str, str, bool]], chat_id: int, persona: str,
) -> tuple[str, list[tuple[str, str]]]:
    """(recap, quotes). One call to the main model, in the chat's persona:
    this posts once a month, so it gets the model that can tell funny from
    long."""
    if not candidates:
        return _FALLBACK_RECAP, []
    listing = "\n".join(
        f"{i}. {'[saved] ' if saved else ''}{name}: "
        f"{' '.join(text.split())[:_POOL_LINE_CHARS]}"
        for i, (name, text, saved) in enumerate(candidates, start=1)
    )
    raw = await openai.chat(
        [
            {"role": "system", "content": persona},
            {"role": "user", "content": MONTHLY_RECAP_PROMPT.format(
                month=month_label, messages=listing,
            )},
        ],
        max_tokens=300, chat_id=chat_id,
    )
    recap, picks = _parse_reply(raw, len(candidates))
    if not recap:
        return _FALLBACK_RECAP, []
    quotes = [
        (candidates[i - 1][0], candidates[i - 1][1]) for i in picks
        if len(candidates[i - 1][1]) <= _MAX_QUOTE_CHARS
    ]
    return recap, quotes


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
    pool = await _fetch_recap_pool(db, chat_id, start_utc, end_utc)
    recap, quotes = await _ai_recap(
        openai, month_label, _candidates(saved, pool), chat_id,
        await _chat_persona(db, chat_id),
    )
    return MonthlyRecapResult(
        month_label=month_label, recap=recap, quotes=quotes, stats=stats,
    )


def render_monthly_recap(result: MonthlyRecapResult) -> str:
    lines = [f"🗓️ {result.month_label} in review", "", result.recap]
    if result.quotes:
        lines.append("")
        for name, text in result.quotes:
            lines.append(f"“{text}” — {name}")
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
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(settings.tzinfo)
    if now.hour < 9:
        # Don't post a recap at midnight the moment the month rolls over —
        # wait for a civilised local hour.
        return
    today = now.date()
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
            log.info("monthly recap posted in chat %s (%s).", chat_id, result.month_label)
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            # Permanent failure (kicked/blocked): stamp anyway so this chat
            # stops costing 4 queries + an AI call every hour for 45 days.
            log.warning("monthly-recap undeliverable for %s (stamping): %s", chat_id, exc)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("monthly-recap send failed for %s (will retry): %s", chat_id, exc)
            continue
        await _stamp(db, chat_id, prev_first)


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
