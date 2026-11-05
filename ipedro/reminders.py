"""Reminder background loop. Fires due reminders into their chat.

Deliberately ignores the admin silence override (silenced_chats): a reminder
is something a user explicitly asked for, not ambient chatter.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from ipedro.bot_messages import track
from ipedro.db.pool import Database

log = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
# The whole token must be duration pieces ("2h30m"), not merely contain one:
# finditer alone read "x5my" as five minutes.
_DURATION_FULL_RE = re.compile(r"(?:\d+\s*[smhdw])+", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
# A year. Beyond it a duration isn't a reminder, and an unbounded one
# overflowed datetime/timedelta inside /remind, /tldr and /shutup, which
# the dispatcher caught and logged as a bare traceback: no reply at all.
MAX_DURATION_SECONDS = 366 * 86400


def parse_duration(token: str) -> int | None:
    """Parse a duration like '5m', '2h30m', '1d', '90s'. Returns seconds, or
    None for anything that isn't one, or is longer than a year."""
    token = (token or "").strip()
    if not token or not _DURATION_FULL_RE.fullmatch(token):
        return None
    total = 0
    for m in _DURATION_RE.finditer(token):
        n, unit = int(m.group(1)), m.group(2).lower()
        total += n * _UNIT_SECONDS[unit]
        if total > MAX_DURATION_SECONDS:
            return None
    if total <= 0:
        return None
    return total


async def add_reminder(
    db: Database, chat_id: int, user_id: int | None, text: str,
    seconds_from_now: int,
) -> int:
    fire_at = datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)
    val = await db.fetchval(
        "INSERT INTO reminders (chat_id, user_id, text, fire_at) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        chat_id, user_id, text, fire_at,
    )
    return int(val)


async def _due_reminders(db: Database) -> list[dict]:
    rows = await db.fetch(
        "SELECT id, chat_id, user_id, text FROM reminders "
        "WHERE fired = FALSE AND fire_at <= NOW() "
        "ORDER BY fire_at ASC LIMIT 50"
    )
    return [dict(r) for r in rows]


async def _mark_fired(db: Database, reminder_id: int) -> None:
    await db.execute(
        "UPDATE reminders SET fired = TRUE WHERE id = $1", reminder_id,
    )


async def run_reminders_loop(
    bot: Bot, db: Database, stop: asyncio.Event,
) -> None:
    log.info("Reminders loop running.")
    while not stop.is_set():
        try:
            due = await _due_reminders(db)
            for r in due:
                # Mark fired only after a successful send, one reminder at a
                # time — a transient Telegram failure must NOT lose the
                # reminder (it retries next tick). A permanent failure (bot
                # kicked/blocked, chat gone) marks it fired so it doesn't
                # retry forever.
                body = f"⏰ Reminder: {r['text']}"
                try:
                    sent = await bot.send_message(r["chat_id"], body)
                except (TelegramForbiddenError, TelegramBadRequest) as exc:
                    log.warning(
                        "Reminder %s undeliverable (dropping): %s", r["id"], exc,
                    )
                except Exception as exc:
                    log.warning(
                        "Reminder %s send failed (will retry): %s", r["id"], exc,
                    )
                    continue
                else:
                    # It IS delivered. Bookkeeping that goes wrong from here
                    # on must not look like a failed send: that re-sent it.
                    try:
                        track(r["chat_id"], sent.message_id, body)
                    except Exception as exc:
                        log.debug("Reminder %s sent but not tracked: %s", r["id"], exc)
                try:
                    await _mark_fired(db, r["id"])
                except Exception as exc:
                    # Delivered but not recorded (a DB blip): it may repeat
                    # once on the next tick. That beats losing it, and the
                    # rest of this batch shouldn't wait a minute behind it.
                    log.warning(
                        "Reminder %s was sent but not marked fired (%s); it "
                        "may be sent once more.", r["id"], exc,
                    )
            wait = 30
        except Exception as exc:
            log.exception("Reminders iteration failed: %s", exc)
            wait = 60
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    log.info("Reminders loop stopped.")
