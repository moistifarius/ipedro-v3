"""Daily comic-strip loop.

For each chat with comic_enabled=TRUE whose last_comic_at is older than
24h, summarise the last 24h of messages into 4 scene descriptions and
render them as a single 4-panel image. Posted with a brief caption.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import BufferedInputFile

from ipedro.bot_messages import track
from ipedro.db.pool import Database
from ipedro.openai_client import OpenAIClient
from ipedro.prompts import COMIC_RENDER_TEMPLATE, COMIC_SCENES_PROMPT

log = logging.getLogger(__name__)

_TICK_SECONDS = 600  # check every 10 min; per-chat cadence is 24h
_LOOKBACK = timedelta(hours=24)


async def _chats_due(db: Database) -> list[int]:
    rows = await db.fetch(
        "SELECT c.chat_id FROM chats c "
        "JOIN chat_config cfg ON cfg.chat_id = c.chat_id "
        "WHERE cfg.comic_enabled = TRUE "
        "  AND (cfg.last_comic_at IS NULL "
        "       OR cfg.last_comic_at < NOW() - INTERVAL '24 hours')"
    )
    return [r["chat_id"] for r in rows]


async def _build_and_post(
    chat_id: int, bot: Bot, db: Database, openai: OpenAIClient,
) -> bool:
    since = datetime.now(timezone.utc) - _LOOKBACK
    rows = await db.fetch(
        "SELECT role, content FROM messages "
        " WHERE chat_id = $1 AND created_at >= $2 "
        " ORDER BY id ASC LIMIT 300",
        chat_id, since,
    )
    if len(rows) < 6:
        log.info("Comic skipped for %s: only %d msgs in 24h.", chat_id, len(rows))
        return False
    joined = "\n".join(f"{r['role']}: {r['content']}" for r in rows)[:12000]
    scenes_text = await openai.short_completion(
        COMIC_SCENES_PROMPT.format(messages=joined),
        max_tokens=300, chat_id=chat_id,
    )
    if not scenes_text:
        return False
    lines = [ln.strip(" -•").strip() for ln in scenes_text.splitlines() if ln.strip()]
    if len(lines) < 4:
        log.info("Comic skipped for %s: scene gen returned %d lines.", chat_id, len(lines))
        return False
    p1, p2, p3, p4 = lines[:4]
    image = await openai.generate_image(
        COMIC_RENDER_TEMPLATE.format(p1=p1, p2=p2, p3=p3, p4=p4),
        chat_id=chat_id,
    )
    if not image:
        return False
    try:
        sent = await bot.send_photo(
            chat_id,
            BufferedInputFile(image, filename="comic.png"),
            caption="📰 The Daily — yesterday in 4 panels.",
            disable_notification=True,
        )
        track(chat_id, sent.message_id, "📰 The Daily comic")
        log.info("Comic posted in %s.", chat_id)
        return True
    except Exception as exc:  # pragma: no cover
        log.warning("Comic send failed for %s: %s", chat_id, exc)
        return False


async def run_comic_loop(
    bot: Bot, db: Database, openai: OpenAIClient, stop: asyncio.Event,
) -> None:
    log.info("Comic loop running.")
    while not stop.is_set():
        try:
            for chat_id in await _chats_due(db):
                ok = await _build_and_post(chat_id, bot, db, openai)
                if ok:
                    await db.execute(
                        "UPDATE chat_config SET last_comic_at = NOW() "
                        "WHERE chat_id = $1",
                        chat_id,
                    )
            wait = _TICK_SECONDS
        except Exception as exc:
            log.exception("Comic loop iteration failed: %s", exc)
            wait = _TICK_SECONDS
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    log.info("Comic loop stopped.")
