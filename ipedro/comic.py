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
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BufferedInputFile

from ipedro.bot_messages import track
from ipedro.db.pool import Database
from ipedro.openai_client import OpenAIClient
from ipedro.prompts import COMIC_RENDER_TEMPLATE, COMIC_SCENES_PROMPT

log = logging.getLogger(__name__)

_TICK_SECONDS = 600  # check every 10 min; per-chat cadence is 24h
_LOOKBACK = timedelta(hours=24)

# What happened to one chat's comic this tick.
POSTED = "posted"                # it went out
SKIPPED = "skipped"              # too quiet a day; nothing was spent
FAILED = "failed"                # spent something, got nothing to post
UNDELIVERABLE = "undeliverable"  # Telegram refuses us in this chat for good

# A chat whose comic failed is due again after this, not on the next 10
# minute tick. Without it a failing chat re-ran the model call AND the
# ~$0.04 image generation every tick: ~144 times a day, until someone
# noticed a warning line.
_RETRY_AFTER_FAILURE = timedelta(hours=6)


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
) -> str:
    since = datetime.now(timezone.utc) - _LOOKBACK
    rows = await db.fetch(
        "SELECT role, content FROM messages "
        " WHERE chat_id = $1 AND created_at >= $2 "
        " ORDER BY id ASC LIMIT 300",
        chat_id, since,
    )
    if len(rows) < 6:
        log.info("Comic skipped for %s: only %d msgs in 24h.", chat_id, len(rows))
        return SKIPPED
    joined = "\n".join(f"{r['role']}: {r['content']}" for r in rows)[:12000]
    scenes_text = await openai.cheap_completion(
        COMIC_SCENES_PROMPT.format(messages=joined),
        max_tokens=300, chat_id=chat_id,
    )
    if not scenes_text:
        return FAILED
    lines = [ln.strip(" -•").strip() for ln in scenes_text.splitlines() if ln.strip()]
    if len(lines) < 4:
        log.info("Comic skipped for %s: scene gen returned %d lines.", chat_id, len(lines))
        return FAILED
    p1, p2, p3, p4 = lines[:4]
    image = await openai.generate_image(
        COMIC_RENDER_TEMPLATE.format(p1=p1, p2=p2, p3=p3, p4=p4),
        chat_id=chat_id,
    )
    if not image:
        return FAILED
    try:
        sent = await bot.send_photo(
            chat_id,
            BufferedInputFile(image, filename="comic.png"),
            caption="📰 The Daily — yesterday in 4 panels.",
            disable_notification=True,
        )
        track(chat_id, sent.message_id, "📰 The Daily comic")
        log.info("Comic posted in %s.", chat_id)
        return POSTED
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        # Kicked, blocked, or media sending restricted: it will fail the
        # same way tomorrow. Treat the day as done rather than retrying.
        log.warning("Comic undeliverable in %s (stamping): %s", chat_id, exc)
        return UNDELIVERABLE
    except Exception as exc:  # pragma: no cover
        log.warning("Comic send failed for %s: %s", chat_id, exc)
        return FAILED


async def _stamp(db: Database, chat_id: int, outcome: str) -> None:
    """Record that this chat has been dealt with, so it isn't picked up
    again next tick. SKIPPED stamps nothing: nothing was spent, and a
    quiet chat gets rechecked cheaply until it has something to draw."""
    if outcome in (POSTED, UNDELIVERABLE):
        await db.execute(
            "UPDATE chat_config SET last_comic_at = NOW() WHERE chat_id = $1",
            chat_id,
        )
    elif outcome == FAILED:
        # Due again in _RETRY_AFTER_FAILURE: 24h cadence minus the wait.
        back = int((_LOOKBACK - _RETRY_AFTER_FAILURE).total_seconds())
        await db.execute(
            "UPDATE chat_config SET last_comic_at = "
            "NOW() - make_interval(secs => $2) WHERE chat_id = $1",
            chat_id, back,
        )


async def run_comic_loop(
    bot: Bot, db: Database, openai: OpenAIClient, stop: asyncio.Event,
) -> None:
    log.info("Comic loop running.")
    while not stop.is_set():
        try:
            for chat_id in await _chats_due(db):
                try:
                    outcome = await _build_and_post(chat_id, bot, db, openai)
                except Exception as exc:
                    log.warning(
                        "Comic build/post failed for chat %s: %s",
                        chat_id, exc,
                    )
                    outcome = FAILED
                try:
                    await _stamp(db, chat_id, outcome)
                except Exception as exc:
                    log.warning("Comic stamp failed for chat %s: %s", chat_id, exc)
            wait = _TICK_SECONDS
        except Exception as exc:
            log.exception("Comic loop iteration failed: %s", exc)
            wait = _TICK_SECONDS
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    log.info("Comic loop stopped.")
