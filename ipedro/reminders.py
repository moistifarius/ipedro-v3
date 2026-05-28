"""Reminder background loop. Fires due reminders into their chat."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from ipedro.db.pool import Database

log = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(token: str) -> int | None:
    """Parse a duration like '5m', '2h30m', '1d', '90s'. Returns seconds, or None."""
    if not token:
        return None
    total = 0
    matched_any = False
    for m in _DURATION_RE.finditer(token):
        matched_any = True
        n, unit = int(m.group(1)), m.group(2).lower()
        total += n * _UNIT_SECONDS[unit]
    if not matched_any or total <= 0:
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


async def _mark_fired(db: Database, ids: list[int]) -> None:
    if not ids:
        return
    await db.execute(
        "UPDATE reminders SET fired = TRUE WHERE id = ANY($1::bigint[])",
        ids,
    )


async def run_reminders_loop(
    bot: Bot, db: Database, stop: asyncio.Event,
) -> None:
    log.info("Reminders loop running.")
    while not stop.is_set():
        try:
            due = await _due_reminders(db)
            fired_ids: list[int] = []
            for r in due:
                try:
                    body = f"⏰ Reminder: {r['text']}"
                    await bot.send_message(r["chat_id"], body)
                except Exception as exc:
                    log.warning(
                        "Reminder %s send failed: %s", r["id"], exc,
                    )
                fired_ids.append(r["id"])
            await _mark_fired(db, fired_ids)
            wait = 30
        except Exception as exc:
            log.exception("Reminders iteration failed: %s", exc)
            wait = 60
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    log.info("Reminders loop stopped.")
