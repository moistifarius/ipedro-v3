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

from ipedro.bot_messages import track
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


# Preserved for an easy revert. Rarity hints are currently disabled —
# rarity_hint() returns "" so spawn messages don't leak tier flavor.
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
    """Rarity is neutralized — return empty so spawn messages no longer
    leak tier flavor. _RARITY_HINTS kept above for easy revert."""
    return ""


# Telltales that the model handed us a cow / dog / other-animal mistake.
# We catch the common shapes and fall back to a hardcoded duck rather
# than spawn a "🐮 QUACK!" embarrassment in chat.
_NOT_A_DUCK_PATTERNS: tuple[str, ...] = (
    "(oo)",     # cow eyes
    "----w",    # cow muzzle/horn
    "/\\/\\",   # cow horns
    "moo",      # cow says moo
    "woof",     # dog says woof
    "meow",     # cat says meow
    "hoot",     # owl
)

# Tiny pool of pre-vetted duck art, used as the fallback whenever the
# AI output fails sanity-check, and rotated occasionally so it doesn't
# always look the same.
_FALLBACK_DUCKS: tuple[str, ...] = (
    "  __\n<('< 🦆 quack!",
    "  _\n>(.)__ quack!\n (___/",
    " __\n<°)))< quack",
    "🦆 quack!",
)


def _looks_like_a_duck(body: str) -> bool:
    """True if the AI output plausibly shows a duck.

    Requires the word 'quack' somewhere (case-insensitive) AND no obvious
    other-animal tells. Cheap heuristic; rejecting a real duck is fine
    (we just fall back to the hardcoded pool)."""
    lower = body.lower()
    if "quack" not in lower:
        return False
    return not any(pat in lower for pat in _NOT_A_DUCK_PATTERNS)


async def build_quack_message(
    openai: OpenAIClient, rarity: str, *,
    is_boss: bool = False, holiday: tuple[str, str] | None = None,
) -> str:
    import random as _random
    msg = await openai.cheap_completion(DUCK_QUACK_PROMPT, max_tokens=120)
    body = (msg or "").strip()
    if not _looks_like_a_duck(body):
        body = _random.choice(_FALLBACK_DUCKS)
    extra: list[str] = []
    if holiday:
        extra.append(f"\n[{holiday[0]} duck — {holiday[1]}]")
    if is_boss:
        extra.append("\n👹 *this one is BIG. one person can't take it alone.*")
    return f"{body}{''.join(extra)}" if extra else body


async def build_quack_message_for(
    openai: OpenAIClient, duck: ActiveDuck,
) -> str:
    return await build_quack_message(
        openai, duck.rarity,
        is_boss=duck.is_boss, holiday=current_holiday(),
    )


async def _recent_activity_factor(db: Database, chat_id: int) -> float:
    """Scale spawn probability by chat activity in the last hour.

    Quiet chats spawn at ~0.4x the base rate; busy chats at up to ~2x.
    Uses a soft log-curve so a single very active chat doesn't dominate.
    """
    val = await db.fetchval(
        "SELECT COUNT(*) FROM messages "
        " WHERE chat_id = $1 AND created_at >= NOW() - INTERVAL '1 hour'",
        chat_id,
    )
    n = int(val or 0)
    # 0 msgs → 0.4, 10 msgs → 1.0, 40 msgs → ~1.5, 200 msgs → ~2.0
    import math
    return 0.4 + min(1.6, math.log1p(n) / math.log1p(40))


async def _maybe_spawn(
    chat_id: int, p_spawn: float, bot: Bot, service: DuckhuntService,
    openai: OpenAIClient, settings: Settings, db: Database,
) -> None:
    factor = await _recent_activity_factor(db, chat_id)
    if random.random() >= min(1.0, p_spawn * factor):
        return
    if await service.active_duck(chat_id):
        return
    duck = await service.spawn_duck(
        chat_id, settings.duckhunt_duck_lifetime_seconds,
    )
    text = await build_quack_message_for(openai, duck)
    try:
        sent = await bot.send_message(chat_id, text, disable_notification=True)
        track(chat_id, sent.message_id, text)
    except Exception as exc:  # pragma: no cover
        log.warning("Failed to deliver quack to %s: %s", chat_id, exc)
    log.info(
        "Spawn announced: chat=%s event_id=%s",
        chat_id, duck.id,
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
                # Announce in each chat so users see the state change instead
                # of silently discovering the duck is gone next time they type
                # `bang` / `bef` / `ignore`.
                for chat_id in departed:
                    try:
                        sent = await bot.send_message(
                            chat_id,
                            "🦆 The duck wandered off.",
                            disable_notification=True,
                        )
                        track(chat_id, sent.message_id, "🦆 The duck wandered off.")
                    except Exception as exc:  # pragma: no cover
                        log.warning(
                            "Wander-off announce failed for chat %s: %s",
                            chat_id, exc,
                        )

            for chat_id in await duckhunt_enabled_chat_ids(db):
                await _maybe_spawn(
                    chat_id, p_spawn, bot, service, openai, settings, db,
                )

            wait = tick
        except Exception as exc:
            log.exception("Spawner iteration failed: %s", exc)
            wait = 60
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    log.info("Duckhunt spawner stopped.")
