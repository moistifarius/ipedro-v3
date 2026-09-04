"""'On this day' nostalgia — resurface what people said on this calendar
day in the past (a month / a few months / a year+ ago).

One entry point: the ``/onthisday`` command (handlers/utility.py), an
on-demand pull for the current chat. (The daily auto-post loop was
replaced by the monthly recap; ``chat_config.on_this_day_enabled`` and
``chat_state.last_on_this_day_date`` are vestigial columns kept only to
avoid a pointless migration.)

The payload is the *verbatim* past messages (that's where the "lol I
forgot I said that" hit comes from); an optional cheap AI line frames
them in-character. Everything degrades gracefully when the AI is down.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from ipedro.config import Settings
from ipedro.db.pool import Database
from ipedro.openai_client import OpenAIClient
from ipedro.prompts import ON_THIS_DAY_PROMPT

log = logging.getLogger(__name__)

# Look-back periods, longest-ago first. The loop posts the OLDEST period
# that actually has substantive messages — the further back, the more
# nostalgic the hit. A young bot will only match the short ones until it
# accrues history, which is fine.
_PERIODS: tuple[tuple[int, str], ...] = (
    (60, "5 years ago today"),   # months → label
    (48, "4 years ago today"),
    (36, "3 years ago today"),
    (24, "2 years ago today"),
    (12, "a year ago today"),
    (6,  "6 months ago today"),
    (3,  "3 months ago today"),
    (1,  "a month ago today"),
)

# A past message has to clear this bar to be worth resurfacing: long
# enough to carry meaning, not a slash-command.
_MIN_MESSAGE_CHARS = 12
_MAX_QUOTES = 3


@dataclass(frozen=True)
class OnThisDayResult:
    label: str                       # e.g. "a year ago today"
    quotes: list[tuple[str, str]]    # (name, text) verbatim
    header: str                      # the AI (or fallback) framing line


def _months_ago(d: date, months: int) -> date:
    """Subtract ``months`` calendar months from ``d``, clamping the day to
    the target month's length (so 31 Mar − 1 month → 28/29 Feb)."""
    total = (d.year * 12 + (d.month - 1)) - months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    # Last day of the target month.
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = (next_month_first - timedelta(days=1)).day
    return date(year, month, min(d.day, last_day))


def _local_day_utc_bounds(
    target: date, tz,
) -> tuple[datetime, datetime]:
    """UTC [start, end) timestamps spanning the local calendar day
    ``target`` in timezone ``tz``. Messages are stored in UTC, so we
    translate the local day into a UTC range for the query."""
    local_start = datetime.combine(target, time.min, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


async def _fetch_day_messages(
    db: Database, chat_id: int, target: date, tz,
) -> list[tuple[str, str]]:
    """Verbatim (name, text) pairs from substantive user messages sent on
    the local calendar day ``target``. Longest first, capped."""
    start_utc, end_utc = _local_day_utc_bounds(target, tz)
    rows = await db.fetch(
        """
        SELECT COALESCE(
                   NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), ''),
                   u.username, 'someone'
               ) AS name,
               m.content AS text
          FROM messages m
          LEFT JOIN users u ON u.user_id = m.user_id
         WHERE m.chat_id = $1
           AND m.role = 'user'
           AND m.created_at >= $2 AND m.created_at < $3
           AND char_length(TRIM(m.content)) >= $4
           AND LEFT(TRIM(m.content), 1) <> '/'
         ORDER BY char_length(m.content) DESC
         LIMIT 12
        """,
        chat_id, start_utc, end_utc, _MIN_MESSAGE_CHARS,
    )
    return [(r["name"], r["text"].strip()) for r in rows]


def _select_quotes(
    candidates: list[tuple[str, str]], rng: random.Random | None = None,
) -> list[tuple[str, str]]:
    """Pick up to _MAX_QUOTES from the candidate pool, sampled so re-runs
    vary. Dedups identical texts and avoids quoting one person twice when
    there's variety to be had."""
    r = rng or random
    seen_text: set[str] = set()
    by_person: list[tuple[str, str]] = []
    extras: list[tuple[str, str]] = []
    pool = list(candidates)
    r.shuffle(pool)
    seen_people: set[str] = set()
    for name, text in pool:
        key = text.lower()
        if key in seen_text:
            continue
        seen_text.add(key)
        if name in seen_people:
            extras.append((name, text))
        else:
            seen_people.add(name)
            by_person.append((name, text))
    chosen = by_person[:_MAX_QUOTES]
    if len(chosen) < _MAX_QUOTES:
        chosen += extras[: _MAX_QUOTES - len(chosen)]
    return chosen


async def build_on_this_day(
    db: Database,
    openai: OpenAIClient,
    settings: Settings,
    chat_id: int,
    *,
    today: date | None = None,
    rng: random.Random | None = None,
) -> OnThisDayResult | None:
    """Core: find the best anniversary for ``chat_id`` and frame it.

    Returns None when no past period has substantive messages (a young or
    quiet chat). Shared by the loop and the /onthisday command.
    """
    tz = settings.tzinfo
    today = today or datetime.now(tz).date()
    for months, label in _PERIODS:
        target = _months_ago(today, months)
        candidates = await _fetch_day_messages(db, chat_id, target, tz)
        if not candidates:
            continue
        quotes = _select_quotes(candidates, rng)
        if not quotes:
            continue
        header = await _frame(openai, label, quotes, chat_id)
        return OnThisDayResult(label=label, quotes=quotes, header=header)
    return None


_FALLBACK_HEADERS: tuple[str, ...] = (
    "Sh-sha. Look what the archive coughed up.",
    "Pulled the tapes. Here's what you said.",
    "Filed under: you, in the past.",
    "The record remembers. Here.",
)


async def _frame(
    openai: OpenAIClient, label: str, quotes: list[tuple[str, str]],
    chat_id: int,
) -> str:
    """One short in-character line over the quotes. Cheap model; static
    fallback if the AI is unavailable so the post still ships."""
    joined = "\n".join(f"- {name}: {text}" for name, text in quotes)
    line = await openai.cheap_completion(
        ON_THIS_DAY_PROMPT.format(when=label, messages=joined[:2000]),
        max_tokens=60, chat_id=chat_id,
    )
    line = (line or "").strip()
    return line or random.choice(_FALLBACK_HEADERS)


def render_on_this_day(result: OnThisDayResult) -> str:
    """The full Telegram message body for an OnThisDayResult."""
    lines = [f"📅 On this day — {result.label}:", ""]
    for name, text in result.quotes:
        # Keep each quote from running away; the payload is the words.
        snippet = text if len(text) <= 280 else text[:279].rstrip() + "…"
        lines.append(f"“{snippet}” — {name}")
    if result.header:
        lines.append("")
        lines.append(result.header)
    return "\n".join(lines)
