"""Idle behavior: the Dude periodically takes a photo and shares it.

Same Poisson shape as the duck spawner. Each tick, every chat that has
opted in (`share_photo_enabled = TRUE`) independently rolls
`P(post this tick) = 1 - exp(-tick / mean_interval)`. So a chat may get
a photo several times in a day or go a week without one — totally random.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time

from aiogram import Bot
from aiogram.types import BufferedInputFile

from ipedro.config import Settings
from ipedro.db.pool import Database
from ipedro.openai_client import OpenAIClient
from ipedro.prompts import (
    PEDRO_PHOTO_CAPTION_PROMPT, PEDRO_PHOTO_RENDER_TEMPLATE,
    PEDRO_PHOTO_SCENE_PROMPT,
)

log = logging.getLogger(__name__)


async def _share_photo_enabled_chat_ids(db: Database) -> list[int]:
    rows = await db.fetch(
        "SELECT c.chat_id FROM chats c "
        "JOIN chat_config cfg ON cfg.chat_id = c.chat_id "
        "WHERE cfg.share_photo_enabled = TRUE"
    )
    return [r["chat_id"] for r in rows]


async def _take_and_post_photo(
    chat_id: int, bot: Bot, openai: OpenAIClient,
) -> None:
    scene = await openai.short_completion(PEDRO_PHOTO_SCENE_PROMPT, max_tokens=60)
    if not scene:
        log.info("Share-photo skipped for chat %s: scene gen unavailable.", chat_id)
        return
    image = await openai.generate_image(
        PEDRO_PHOTO_RENDER_TEMPLATE.format(scene=scene),
    )
    if not image:
        log.info("Share-photo skipped for chat %s: image gen unavailable.", chat_id)
        return
    caption = await openai.short_completion(
        PEDRO_PHOTO_CAPTION_PROMPT.format(scene=scene), max_tokens=60,
    )
    caption = (caption or scene).strip()[:1000]
    try:
        await bot.send_photo(
            chat_id,
            BufferedInputFile(image, filename="dude.png"),
            caption=caption,
            disable_notification=True,
        )
        log.info("Share-photo posted in chat %s: %s", chat_id, scene)
    except Exception as exc:  # pragma: no cover
        log.warning("Failed to send share-photo to %s: %s", chat_id, exc)


async def run_share_photo_loop(
    bot: Bot, db: Database, openai: OpenAIClient, settings: Settings,
    stop: asyncio.Event,
) -> None:
    """Loop until `stop` is set."""
    tick = max(1, settings.share_photo_tick_seconds)
    mean = max(tick, settings.share_photo_mean_interval_seconds)
    p_post = 1.0 - math.exp(-tick / mean)
    log.info(
        "Share-photo loop running. tick=%ss mean_interval=%ss p_post_per_tick=%.5f",
        tick, mean, p_post,
    )
    while not stop.is_set():
        try:
            for chat_id in await _share_photo_enabled_chat_ids(db):
                if random.random() >= p_post:
                    continue
                await _take_and_post_photo(chat_id, bot, openai)
            wait = tick
        except Exception as exc:
            log.exception("Share-photo iteration failed: %s", exc)
            wait = 60
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    log.info("Share-photo loop stopped.")
