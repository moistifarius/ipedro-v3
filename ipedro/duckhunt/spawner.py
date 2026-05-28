"""Background duck spawner.

A single asyncio task per running bot. Each tick, for every duckhunt-enabled
chat without an active duck, it independently rolls a per-tick spawn
probability derived from the configured mean inter-arrival time:

    P(spawn this tick) = 1 - exp(-tick / mean_interval)

This is a Poisson process per chat — no fixed window, no guaranteed cadence.
Natural consequences:
  - Sometimes several ducks an hour (tight clusters).
  - Sometimes nothing for days (long gaps).
Each tick also rolls a probabilistic departure check for any currently active
duck, so ducks may wander off at any time.
Restart-safe: state lives in Postgres; the in-memory task is just a tick.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time

from aiogram import Bot

from ipedro.config import Settings
from ipedro.db.pool import Database
from ipedro.duckhunt.scoring import current_holiday
from ipedro.duckhunt.service import ActiveDuck, DuckhuntService
from ipedro.openai_client import OpenAIClient
from ipedro.prompts import DUCK_QUACK_PROMPT

log = logging.getLogger(__name__)


async def duckhunt_enabled_chat_ids(db: Database) -> list[int]:
    rows = await db.fetch(
        "SELECT c.chat_id FROM chats c "
        "JOIN chat_config cfg ON cfg.chat_id = c.chat_id "
        "WHERE cfg.duckhunt_enabled = TRUE"
    )
    return [r["chat_id"] for r in rows]


async def duckhunt_enabled_chats(db: Database) -> list[dict]:
    """Return enabled chats with chat_id, title, type so a UI can label them."""
    rows = await db.fetch(
        "SELECT c.chat_id, c.title, c.type FROM chats c "
        "JOIN chat_config cfg ON cfg.chat_id = c.chat_id "
        "WHERE cfg.duckhunt_enabled = TRUE "
        "ORDER BY c.last_seen DESC"
    )
    return [dict(r) for r in rows]


# Quirky, vibes-only hints. Each tier has a few options; one is picked per
# spawn. The exact rarity is never named — the flavor lets observant
# players guess without making it a plain announcement.
_RARITY_HINTS: dict[str, tuple[str, ...]] = {
    "common": (
        "",
        " (just a duck)",
        " (perfectly average)",
    ),
    "uncommon": (
        " (one feather looks oddly shiny)",
        " (it carries itself with confidence)",
        " ✨",
    ),
    "rare": (
        " ✨ (something glints)",
        " (the light bends a little around it)",
        " (you swear it just winked)",
    ),
    "epic": (
        " ✨✨ (it's GLOWING. that's not normal.)",
        " (the colors on its feathers keep shifting)",
        " 💫 (you feel briefly important)",
    ),
    "legendary": (
        " 👑💎✨ (the air HUMS around it)",
        " ✨💎 (you forget your name for a second)",
        " 👑 (somehow it is wearing a crown)",
        " (reality wobbles. there is a duck.)",
    ),
}


def rarity_hint(rarity: str) -> str:
    """Pick a flavor-only hint for a given rarity. Empty string for some."""
    return random.choice(_RARITY_HINTS.get(rarity, ("",)))


async def build_quack_message(
    openai: OpenAIClient, rarity: str, *,
    is_boss: bool = False, holiday: tuple[str, str] | None = None,
) -> str:
    msg = await openai.short_completion(DUCK_QUACK_PROMPT, max_tokens=120)
    body = (msg or "🦆 quack!").strip()
    hint = rarity_hint(rarity)
    extra: list[str] = []
    if holiday:
        extra.append(f"\n[{holiday[0]} duck — {holiday[1]}]")
    if is_boss:
        extra.append("\n👹 *this one is BIG. one person can't take it alone.*")
    return f"{body}{hint}{''.join(extra)}" if hint or extra else body


async def build_quack_message_for(
    openai: OpenAIClient, duck: ActiveDuck,
) -> str:
    return await build_quack_message(
        openai, duck.rarity,
        is_boss=duck.is_boss, holiday=current_holiday(),
    )


async def _maybe_spawn(
    chat_id: int, p_spawn: float, bot: Bot, service: DuckhuntService,
    openai: OpenAIClient, settings: Settings,
) -> None:
    if random.random() >= p_spawn:
        return
    if await service.active_duck(chat_id):
        return
    duck = await service.spawn_duck(
        chat_id, settings.duckhunt_duck_lifetime_seconds,
    )
    text = await build_quack_message_for(openai, duck)
    try:
        await bot.send_message(chat_id, text, disable_notification=True)
    except Exception as exc:  # pragma: no cover
        log.warning("Failed to deliver quack to %s: %s", chat_id, exc)
    log.info(
        "Spawn announced: chat=%s rarity=%s event_id=%s",
        chat_id, duck.rarity, duck.id,
    )


async def run_spawner(
    bot: Bot, db: Database, openai: OpenAIClient, settings: Settings,
    stop: asyncio.Event,
) -> None:
    """Loop until `stop` is set."""
    service = DuckhuntService(db)
    tick = max(1, settings.duckhunt_spawn_tick_seconds)
    mean = max(tick, settings.duckhunt_mean_spawn_interval_seconds)
    p_spawn = 1.0 - math.exp(-tick / mean)
    log.info(
        "Duckhunt spawner running. tick=%ss mean_interval=%ss p_spawn_per_tick=%.4f",
        tick, mean, p_spawn,
    )
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

            for chat_id in await duckhunt_enabled_chat_ids(db):
                await _maybe_spawn(chat_id, p_spawn, bot, service, openai, settings)

            wait = tick
        except Exception as exc:
            log.exception("Spawner iteration failed: %s", exc)
            wait = 60
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    log.info("Duckhunt spawner stopped.")
