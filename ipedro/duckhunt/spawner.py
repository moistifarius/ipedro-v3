"""Background duck spawner.

A single asyncio task per running bot that periodically picks a chat with
duckhunt enabled and spawns a duck there, then sleeps for a random interval.
Each tick also rolls a probabilistic departure check for any currently
active duck, so ducks may wander off at any time (more likely as time
passes; ~98% gone by 24h with the default half-life).
Restart-safe: state lives in Postgres, the in-memory task is just a tick.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from aiogram import Bot

from ipedro.config import Settings
from ipedro.db.pool import Database
from ipedro.duckhunt.service import DuckhuntService
from ipedro.openai_client import OpenAIClient
from ipedro.prompts import DUCK_QUACK_PROMPT

log = logging.getLogger(__name__)


async def _duckhunt_enabled_chat_ids(db: Database) -> list[int]:
    rows = await db.fetch(
        "SELECT c.chat_id FROM chats c "
        "JOIN chat_config cfg ON cfg.chat_id = c.chat_id "
        "WHERE cfg.duckhunt_enabled = TRUE"
    )
    return [r["chat_id"] for r in rows]


async def _build_quack_message(openai: OpenAIClient) -> str:
    msg = await openai.short_completion(DUCK_QUACK_PROMPT, max_tokens=120)
    return (msg or "🦆 quack!").strip()


async def run_spawner(
    bot: Bot, db: Database, openai: OpenAIClient, settings: Settings,
    stop: asyncio.Event,
) -> None:
    """Loop until `stop` is set."""
    service = DuckhuntService(db)
    log.info("Duckhunt spawner running.")
    last_tick = time.monotonic()
    while not stop.is_set():
        try:
            tick_seconds = max(1.0, time.monotonic() - last_tick)
            last_tick = time.monotonic()

            await service.expire_old_ducks()
            departed = await service.probabilistically_depart(
                tick_seconds=tick_seconds,
                half_life_seconds=settings.duckhunt_duck_half_life_seconds,
            )
            if departed:
                log.info("Probabilistic departure: %d duck(s) wandered off.", len(departed))

            chat_ids = await _duckhunt_enabled_chat_ids(db)
            if chat_ids:
                target = random.choice(chat_ids)
                if not await service.active_duck(target):
                    duck = await service.spawn_duck(
                        target, settings.duckhunt_duck_lifetime_seconds,
                    )
                    # Rarity is deliberately NOT broadcast on spawn (per UX).
                    text = await _build_quack_message(openai)
                    try:
                        await bot.send_message(
                            target, text, disable_notification=True,
                        )
                    except Exception as exc:  # pragma: no cover
                        log.warning("Failed to deliver quack to %s: %s", target, exc)
                    # Log the rarity at INFO so /logs can show it to admins.
                    log.info(
                        "Spawn announced: chat=%s rarity=%s event_id=%s",
                        target, duck.rarity, duck.id,
                    )
            wait = random.randint(
                settings.duckhunt_min_spawn_seconds,
                settings.duckhunt_max_spawn_seconds,
            )
        except Exception as exc:
            log.exception("Spawner iteration failed: %s", exc)
            wait = 60
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    log.info("Duckhunt spawner stopped.")
