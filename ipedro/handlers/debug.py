"""Admin-gated /debug_* commands to force-trigger the new behaviors.

These exist purely so the admin can verify each path end-to-end without
waiting on Poisson clocks or random outcomes. They are NOT registered if
the caller isn't an admin (require_admin handles that silently in groups).
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ipedro.duckhunt.spawner import rarity_hint
from ipedro.handlers.common import display_name, require_admin
from ipedro.handlers.duckhunt import _issue_bef_challenge
from ipedro.runtime import Runtime
from ipedro.sharephoto import _take_and_post_photo

log = logging.getLogger(__name__)

_HELP = (
    "Debug commands (admin only):\n"
    "  /debug_help — this list\n"
    "  /debug_sharephoto — force Pedro to generate + post a photo now\n"
    "  /debug_challenge — issue a random bef challenge (captcha|trivia|recipe)\n"
    "  /debug_captcha — issue a captcha challenge\n"
    "  /debug_trivia — issue a trivia challenge\n"
    "  /debug_recipe — issue a recipe challenge\n"
    "  /debug_duck — alias for /duckhunt (force-spawn in this chat)\n"
    "\nFor the others, just type the trigger:\n"
    "  say 'pedro' anywhere — Pedro should reply\n"
    "  say 'cat' / 'kitty' / 🐈 — Pedro drops a dubious cat fact\n"
    "  type 'bang' twice within 15s — second one trips the cooldown challenge"
)


def build_router(rt: Runtime) -> Router:
    r = Router(name="debug")

    @r.message(Command("debug_help"))
    async def debug_help(msg: Message) -> None:
        if not await require_admin(msg, rt.settings.admin_ids):
            return
        await msg.reply(_HELP, disable_notification=True)

    @r.message(Command("debug_sharephoto"))
    async def debug_sharephoto(msg: Message) -> None:
        if not await require_admin(msg, rt.settings.admin_ids):
            return
        await msg.reply("Generating photo…", disable_notification=True)
        await _take_and_post_photo(msg.chat.id, rt.bot, rt.openai)

    async def _force_challenge(msg: Message, kind: str | None) -> None:
        if not await require_admin(msg, rt.settings.admin_ids):
            return
        who = display_name(msg.from_user) if msg.from_user else "anonymous"
        intro = (
            f"[debug] Forcing a {kind or 'random'} challenge."
        )
        issued = await _issue_bef_challenge(
            rt, msg, who, intro=intro, force_kind=kind,
        )
        if not issued:
            await msg.reply(
                "Failed to issue challenge (AI unavailable?).",
                disable_notification=True,
            )

    @r.message(Command("debug_challenge"))
    async def debug_challenge_random(msg: Message) -> None:
        await _force_challenge(msg, kind=None)

    @r.message(Command("debug_captcha"))
    async def debug_captcha(msg: Message) -> None:
        await _force_challenge(msg, kind="captcha")

    @r.message(Command("debug_trivia"))
    async def debug_trivia(msg: Message) -> None:
        await _force_challenge(msg, kind="trivia")

    @r.message(Command("debug_recipe"))
    async def debug_recipe(msg: Message) -> None:
        await _force_challenge(msg, kind="recipe")

    @r.message(Command("debug_duck"))
    async def debug_duck(msg: Message) -> None:
        if not await require_admin(msg, rt.settings.admin_ids):
            return
        if await rt.duckhunt.active_duck(msg.chat.id):
            await msg.reply("Duck already active.", disable_notification=True)
            return
        duck = await rt.duckhunt.spawn_duck(
            msg.chat.id, rt.settings.duckhunt_duck_lifetime_seconds,
        )
        hint = rarity_hint(duck.rarity)
        await msg.reply(
            f"🦆 quack!{hint}\n[debug] actual rarity: {duck.rarity}",
            disable_notification=True,
        )

    return r
