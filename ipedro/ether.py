"""Ether: pager-style cross-chat message garbling.

Every ~hour, with low probability, picks a recent message from one
opted-in chat, runs it through a pager-style garble (dropped chars,
substitutions, ALL CAPS, blackouts, truncation), and broadcasts it
into a *different* opted-in chat with a spooky wrapper.

The chats are linked only by their consent — neither side knows who's
on the other end. From the receiver's POV it's a stray transmission
they happen to be picking up out of the ether.

Opt-in: ``chat_config.ether_enabled`` (default off).

Cooldown: receiving chats won't get a transmission more than once per
4 hours (``chat_state.last_ether_at``). Sending isn't rate-limited
beyond the loop tick × dice roll — a chatty source chat is fine since
each broadcast goes to a randomly-chosen receiver anyway.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from ipedro.bot_messages import track
from ipedro.db.pool import Database
from ipedro.silenced_chats import is_silenced

log = logging.getLogger(__name__)

_TICK_SECONDS = 3600          # 1h
_DROP_CHANCE = 0.30           # per tick → roughly one broadcast every ~3h
_RECEIVER_COOLDOWN = timedelta(hours=4)
_SOURCE_LOOKBACK = timedelta(hours=24)
_MIN_MESSAGE_LEN = 25         # don't pick "k", "lol", etc.
_MAX_MESSAGE_LEN = 240        # pager-style: alphanumeric pagers held ~240 chars

_WRAPPERS = (
    "📟 *beep* *beep* incoming\n\n{body}",
    "📟 [transmission from the ether]\n\n{body}",
    "📟 ▓▓▓ signal acquired ▓▓▓\n\n{body}",
    "📟 ...catching a stray page...\n\n{body}",
    "📟 MSG-{code}\n{body}",
)

_SUBS = {
    "o": "0", "i": "1", "e": "3", "s": "5", "a": "@",
    "l": "1", "t": "7", "b": "8", "g": "9",
}


def garble_pager(text: str, *, rng: random.Random | None = None) -> str:
    """Pure-python pager-style garble. Deterministic when ``rng`` is seeded.

    Char-level: low-rate drops and leet substitutions. Word-level: caps
    and blackouts. Whole-string: possible trailing truncation. Always
    returns at least ``***`` so callers don't have to handle empty.
    """
    r = rng if rng is not None else random
    text = (text or "").strip()[:_MAX_MESSAGE_LEN]
    if not text:
        return "***"

    out_chars: list[str] = []
    for c in text:
        roll = r.random()
        if roll < 0.04:                 # drop
            continue
        if roll < 0.14 and c.isalpha():
            sub = _SUBS.get(c.lower())
            if sub is not None:
                out_chars.append(sub)
                continue
        out_chars.append(c)
    s = "".join(out_chars)

    words = s.split()
    transformed: list[str] = []
    for w in words:
        roll = r.random()
        if roll < 0.05:
            transformed.append("***")
        elif roll < 0.40:
            transformed.append(w.upper())
        else:
            transformed.append(w)
    s = " ".join(transformed)

    if r.random() < 0.25 and len(s) > 30:
        s = s[: r.randint(20, len(s) - 5)] + "…"
    return s.strip() or "***"


def _wrap(body: str, *, rng: random.Random | None = None) -> str:
    r = rng if rng is not None else random
    template = r.choice(_WRAPPERS)
    return template.format(body=body, code=f"{r.randint(0, 9999):04d}")


async def _opted_in_chats(db: Database) -> list[int]:
    rows = await db.fetch(
        "SELECT chat_id FROM chat_config WHERE ether_enabled = TRUE",
    )
    return [r["chat_id"] for r in rows]


async def _pick_source_message(
    db: Database, source_chat_ids: list[int],
) -> tuple[int, str] | None:
    """Return (source_chat_id, message_text) or None if nothing eligible.

    Eligible = within lookback, role='user' (skip the bot's own + system),
    long enough to be interesting. Random pick across all eligible rows
    so a quiet chat with one good message has the same odds as a chatty
    one with twenty.
    """
    if not source_chat_ids:
        return None
    since = datetime.now(timezone.utc) - _SOURCE_LOOKBACK
    row = await db.fetchrow(
        "SELECT chat_id, content FROM messages "
        " WHERE chat_id = ANY($1::bigint[]) "
        "   AND role = 'user' "
        "   AND created_at >= $2 "
        "   AND char_length(content) >= $3 "
        " ORDER BY random() LIMIT 1",
        source_chat_ids, since, _MIN_MESSAGE_LEN,
    )
    if row is None:
        return None
    return int(row["chat_id"]), row["content"]


async def _pick_destination(
    db: Database, all_opted_in: list[int], exclude: int,
) -> int | None:
    """Pick a different opted-in chat that isn't on receiver cooldown."""
    candidates = [c for c in all_opted_in if c != exclude]
    if not candidates:
        return None
    cutoff = datetime.now(timezone.utc) - _RECEIVER_COOLDOWN
    rows = await db.fetch(
        "SELECT c.chat_id FROM chats c "
        "LEFT JOIN chat_state cs ON cs.chat_id = c.chat_id "
        " WHERE c.chat_id = ANY($1::bigint[]) "
        "   AND (cs.last_ether_at IS NULL OR cs.last_ether_at < $2)",
        candidates, cutoff,
    )
    eligible = [int(r["chat_id"]) for r in rows]
    if not eligible:
        return None
    return random.choice(eligible)


async def _maybe_broadcast(bot: Bot, db: Database) -> None:
    if random.random() >= _DROP_CHANCE:
        return
    opted_in = await _opted_in_chats(db)
    if len(opted_in) < 2:
        # Need at least two opted-in chats for a cross-broadcast.
        return
    picked = await _pick_source_message(db, opted_in)
    if picked is None:
        return
    source_id, raw = picked
    dest_id = await _pick_destination(db, opted_in, exclude=source_id)
    if dest_id is None:
        return
    body = garble_pager(raw)
    text = _wrap(body)
    try:
        sent = await bot.send_message(
            dest_id, text,
            disable_notification=is_silenced(dest_id),
        )
        track(dest_id, sent.message_id, text)
        await db.execute(
            "INSERT INTO chat_state (chat_id, last_ether_at) "
            "VALUES ($1, NOW()) "
            "ON CONFLICT (chat_id) DO UPDATE "
            "SET last_ether_at = EXCLUDED.last_ether_at",
            dest_id,
        )
        log.info("Ether broadcast: %s → %s.", source_id, dest_id)
    except Exception as exc:  # pragma: no cover
        log.warning("Ether broadcast failed (%s → %s): %s", source_id, dest_id, exc)


async def run_ether_loop(bot: Bot, db: Database, stop: asyncio.Event) -> None:
    log.info("Ether loop running.")
    while not stop.is_set():
        try:
            await _maybe_broadcast(bot, db)
            wait = _TICK_SECONDS
        except Exception as exc:
            log.exception("Ether loop iteration failed: %s", exc)
            wait = _TICK_SECONDS
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    log.info("Ether loop stopped.")
