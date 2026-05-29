"""Daily celebrations loop: posts a message in each chat that has
birthdays / anniversaries matching today's UTC date.

Ticks every few minutes. For each match it hasn't celebrated today yet,
it posts a short note and stamps `last_celebrated = today` so it won't
re-fire. Restart-safe.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

from aiogram import Bot

from ipedro.bot_messages import track
from ipedro.db.pool import Database
from ipedro.silenced_chats import is_silenced

log = logging.getLogger(__name__)

_TICK_SECONDS = 300  # 5 min


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _build_message(row: dict, name: str) -> str:
    label = row["label"]
    note = f" ({row['note']})" if row.get("note") else ""
    if label == "birthday":
        if row["year"]:
            age = _today_utc().year - row["year"]
            return f"🎂 Happy birthday to {name}! ({age} today){note}"
        return f"🎂 Happy birthday to {name}!{note}"
    if label == "anniversary":
        if row["year"]:
            years = _today_utc().year - row["year"]
            return f"🎉 Anniversary today: {name} — {years} year(s).{note}"
        return f"🎉 Anniversary today: {name}.{note}"
    return f"🎉 Today is a {label} for {name}.{note}"


async def _due_today(db: Database) -> list[dict]:
    today = _today_utc()
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
    bot: Bot, db: Database, stop: asyncio.Event,
) -> None:
    log.info("Celebrations loop running.")
    while not stop.is_set():
        try:
            today = _today_utc()
            for row in await _due_today(db):
                name = row.get("name") or "someone"
                chat_id = row["chat_id"]
                text = _build_message(row, name)
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
