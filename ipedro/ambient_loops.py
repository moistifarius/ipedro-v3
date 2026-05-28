"""Misc ambient background loops.

- Confession surfacing: every ~1h, with low probability, pick an unsurfaced
  confession and post it into a random known chat.
- Yearly retrospective: when the date is Dec 31 (UTC) and a chat hasn't
  had a retrospective yet this year, post one.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from aiogram import Bot

from ipedro.db.pool import Database
from ipedro.openai_client import OpenAIClient
from ipedro.prompts import FORTUNE_PROMPT, YEAR_RETRO_PROMPT

log = logging.getLogger(__name__)

_TICK_SECONDS = 3600  # 1 hour
_CONFESSION_DROP_CHANCE = 0.25   # per tick, ~ once every 4h on average


async def _maybe_surface_confession(bot: Bot, db: Database) -> None:
    if random.random() >= _CONFESSION_DROP_CHANCE:
        return
    conf = await db.fetchrow(
        "SELECT id, text FROM confessions "
        "WHERE surfaced_at IS NULL ORDER BY random() LIMIT 1"
    )
    if not conf:
        return
    chats = await db.fetch(
        "SELECT chat_id FROM chats "
        "WHERE last_seen >= NOW() - INTERVAL '14 days'"
    )
    if not chats:
        return
    chat_id = random.choice(chats)["chat_id"]
    try:
        await bot.send_message(
            chat_id,
            f"📩 An anonymous confession from somewhere:\n\n{conf['text']}",
        )
        await db.execute(
            "UPDATE confessions SET surfaced_at = NOW() WHERE id = $1",
            conf["id"],
        )
        log.info("Confession %s surfaced into chat %s.", conf["id"], chat_id)
    except Exception as exc:  # pragma: no cover
        log.warning("Confession surface failed for %s: %s", chat_id, exc)


async def _maybe_yearly_retro(
    bot: Bot, db: Database, openai: OpenAIClient,
) -> None:
    now = datetime.now(timezone.utc)
    if now.month != 12 or now.day != 31:
        return
    year = now.year
    chats = await db.fetch(
        "SELECT c.chat_id FROM chats c "
        "LEFT JOIN chat_state cs ON cs.chat_id = c.chat_id "
        "WHERE COALESCE(cs.last_retrospective_year, 0) < $1",
        year,
    )
    for c in chats:
        chat_id = c["chat_id"]
        # Pull this year's running summaries + recent messages as context.
        rows = await db.fetch(
            "SELECT summary FROM summaries "
            "WHERE chat_id = $1 AND EXTRACT(YEAR FROM created_at)::int = $2 "
            "ORDER BY id ASC LIMIT 40",
            chat_id, year,
        )
        if not rows:
            continue
        joined = "\n\n".join(r["summary"] for r in rows)[:14000]
        retro = await openai.short_completion(
            YEAR_RETRO_PROMPT.format(messages=joined),
            max_tokens=600, chat_id=chat_id,
        )
        if not retro:
            continue
        try:
            await bot.send_message(
                chat_id,
                f"🎆 {year} — A Year in Review 🎆\n\n{retro}",
            )
            await db.execute(
                "INSERT INTO chat_state (chat_id, last_retrospective_year) "
                "VALUES ($1, $2) "
                "ON CONFLICT (chat_id) DO UPDATE "
                "SET last_retrospective_year = EXCLUDED.last_retrospective_year",
                chat_id, year,
            )
            log.info("Posted %d retrospective in chat %s.", year, chat_id)
        except Exception as exc:  # pragma: no cover
            log.warning("Retro send failed for %s: %s", chat_id, exc)


async def _maybe_daily_fortune(
    bot: Bot, db: Database, openai: OpenAIClient,
) -> None:
    """Once per UTC day per opted-in chat, post a short fortune."""
    today = datetime.now(timezone.utc).date()
    chats = await db.fetch(
        "SELECT c.chat_id FROM chats c "
        "JOIN chat_config cfg ON cfg.chat_id = c.chat_id "
        "LEFT JOIN chat_state cs ON cs.chat_id = c.chat_id "
        "WHERE cfg.fortune_enabled = TRUE "
        "  AND (cs.last_fortune_date IS NULL OR cs.last_fortune_date < $1)",
        today,
    )
    for c in chats:
        chat_id = c["chat_id"]
        fortune = await openai.short_completion(
            FORTUNE_PROMPT, max_tokens=60, chat_id=chat_id,
        )
        if not fortune:
            continue
        try:
            await bot.send_message(chat_id, f"🥠 {fortune}")
            await db.execute(
                "INSERT INTO chat_state (chat_id, last_fortune_date) "
                "VALUES ($1, $2) "
                "ON CONFLICT (chat_id) DO UPDATE "
                "SET last_fortune_date = EXCLUDED.last_fortune_date",
                chat_id, today,
            )
            log.info("Fortune posted in chat %s.", chat_id)
        except Exception as exc:  # pragma: no cover
            log.warning("Fortune send failed for %s: %s", chat_id, exc)


async def run_ambient_loops(
    bot: Bot, db: Database, openai: OpenAIClient, stop: asyncio.Event,
) -> None:
    log.info("Ambient loops running.")
    while not stop.is_set():
        try:
            await _maybe_surface_confession(bot, db)
            await _maybe_yearly_retro(bot, db, openai)
            await _maybe_daily_fortune(bot, db, openai)
            wait = _TICK_SECONDS
        except Exception as exc:
            log.exception("Ambient loop iteration failed: %s", exc)
            wait = _TICK_SECONDS
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    log.info("Ambient loops stopped.")
