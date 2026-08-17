"""Daily celebrations loop: posts a message in each chat that has
birthdays / anniversaries matching today's LOCAL date (settings.tzinfo).

Ticks every few minutes. For each match it hasn't celebrated today yet,
it posts a short note and stamps `last_celebrated = today` so it won't
re-fire. Restart-safe.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

from aiogram import Bot

from ipedro.bot_messages import track
from ipedro.config import Settings
from ipedro.db.pool import Database
from ipedro.silenced_chats import is_silenced

log = logging.getLogger(__name__)

_TICK_SECONDS = 300  # 5 min




def _build_message(row: dict, name: str, today: date) -> str:
    # Named anniversaries are stored as "anniversary:<name>" so they don't
    # overwrite each other; celebrate them all as anniversaries.
    label = row["label"].split(":", 1)[0]
    note = f" ({row['note']})" if row.get("note") else ""
    if label == "birthday":
        if row["year"]:
            age = today.year - row["year"]
            return f"🎂 Happy birthday to {name}! ({age} today){note}"
        return f"🎂 Happy birthday to {name}!{note}"
    if label == "anniversary":
        if row["year"]:
            years = today.year - row["year"]
            return f"🎉 Anniversary today: {name} — {years} year(s).{note}"
        return f"🎉 Anniversary today: {name}.{note}"
    return f"🎉 Today is a {label} for {name}.{note}"


async def _due_today(db: Database, today: date) -> list[dict]:
    rows = await db.fetch(
        "SELECT cd.id, cd.chat_id, cd.user_id, cd.label, cd.month, cd.day, "
        "       cd.year, cd.note, "
        "       COALESCE(u.first_name, u.username) AS name "
        "  FROM chat_dates cd "
        "  LEFT JOIN users u ON u.user_id = cd.user_id "
        " WHERE cd.month = $1 AND cd.day = $2 "
        "   AND (cd.last_celebrated IS NULL OR cd.last_celebrated < $3)",
        today.month, today.day, today,
    )
    return [dict(r) for r in rows]


async def _stamp_celebrated(db: Database, row_id: int, when: date) -> None:
    await db.execute(
        "UPDATE chat_dates SET last_celebrated = $1 WHERE id = $2",
        when, row_id,
    )


async def run_celebrations_loop(
    bot: Bot, db: Database, settings: Settings, stop: asyncio.Event,
) -> None:
    log.info("Celebrations loop running.")
    while not stop.is_set():
        try:
            # Local calendar date — a UTC date would fire birthdays at
            # 5pm the day before in Pacific time.
            today = datetime.now(settings.tzinfo).date()
            for row in await _due_today(db, today):
                name = row.get("name") or "someone"
                chat_id = row["chat_id"]
                text = _build_message(row, name, today)
                try:
                    sent = await bot.send_message(
                        chat_id, text,
                        disable_notification=is_silenced(chat_id),
                    )
                    track(chat_id, sent.message_id, text)
                    await _stamp_celebrated(db, row["id"], today)
                except Exception as exc:
                    log.warning(
                        "Celebration send failed for chat %s: %s",
                        chat_id, exc,
                    )
            wait = _TICK_SECONDS
        except Exception as exc:
            log.exception("Celebrations iteration failed: %s", exc)
            wait = _TICK_SECONDS
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    log.info("Celebrations loop stopped.")
