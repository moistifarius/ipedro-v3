"""Duckhunt slash commands and 'bang/bef/ignore' triggers."""

from __future__ import annotations

import logging
import random

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from ipedro.duckhunt.captcha_gen import make_captcha
from ipedro.duckhunt.spawner import rarity_hint
from ipedro.duckhunt.verdicts import parse_verdict
from ipedro.handlers.common import display_name, get_or_create_chat_config
from ipedro.prompts import (
    DUCK_BEF_CHALLENGE_PROMPT, DUCK_BEF_DECIDE_PROMPT,
)
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

_CHALLENGE_KINDS = ("captcha", "trivia", "recipe")


async def _issue_bef_challenge(
    rt: Runtime, msg: Message, who: str, *, intro: str,
    force_kind: str | None = None,
) -> bool:
    """Generate a captcha/trivia/recipe challenge and store it as pending.

    Captcha is a real image captcha rendered server-side; the stored
    `challenge` text IS the answer (judged by exact match later). Trivia
    and recipe are AI-generated text challenges judged by an AI prompt.
    Returns True if the challenge was issued; False otherwise.
    """
    kind = force_kind if force_kind in _CHALLENGE_KINDS else random.choice(_CHALLENGE_KINDS)
    if kind == "captcha":
        answer, png = make_captcha()
        try:
            prompt_msg = await msg.answer_photo(
                BufferedInputFile(png, filename="captcha.png"),
                caption=(
                    f"{intro} Solve this captcha to try again — reply to "
                    f"this message with the text in the image."
                ),
                disable_notification=True,
            )
        except Exception as exc:
            log.warning("Failed to send captcha to %s: %s", msg.chat.id, exc)
            return False
        await rt.duckhunt.set_bef_challenge(
            msg.chat.id, msg.from_user.id, answer, kind, prompt_msg.message_id,
        )
        return True

    challenge_text = await rt.openai.short_completion(
        DUCK_BEF_CHALLENGE_PROMPT.format(display_name=who, kind=kind),
        max_tokens=200,
    )
    if not challenge_text:
        log.info(
            "Skipping bef challenge for %s/%s - AI unavailable.",
            msg.chat.id, msg.from_user.id if msg.from_user else None,
        )
        return False
    prompt_msg = await msg.answer(
        f"{intro} Reply to this message with the answer to try again:\n\n"
        f"{challenge_text}",
        disable_notification=True,
    )
    await rt.duckhunt.set_bef_challenge(
        msg.chat.id, msg.from_user.id, challenge_text, kind,
        prompt_msg.message_id,
    )
    return True


def build_router(rt: Runtime) -> Router:
    r = Router(name="duckhunt")

    @r.message(Command("duckhunt"))
    async def duckhunt_cmd(msg: Message) -> None:
        """Force-spawn a duck in this chat (requires duckhunt enabled)."""
        cfg = await get_or_create_chat_config(rt, msg)
        if not cfg.duckhunt_enabled:
            await msg.reply(
                "Duckhunt isn't enabled here. /chat_config duckhunt on",
                disable_notification=True,
            )
            return
        if await rt.duckhunt.active_duck(msg.chat.id):
            await msg.reply(
                "There's already a duck around!",
                disable_notification=True,
            )
            return
        duck = await rt.duckhunt.spawn_duck(
            msg.chat.id, rt.settings.duckhunt_duck_lifetime_seconds,
        )
        log.info(
            "Manual spawn in chat %s by user %s -> rarity=%s",
            msg.chat.id, msg.from_user.id if msg.from_user else None, duck.rarity,
        )
        hint = rarity_hint(duck.rarity)
        await msg.answer(
            f"🦆 quack!{hint}" if hint else "🦆 quack!",
            disable_notification=True,
        )

    @r.message(Command("quackflag"))
    async def quackflag(msg: Message) -> None:
        active = await rt.duckhunt.active_duck(msg.chat.id)
        if active:
            await msg.reply("Duck status: ACTIVE.", disable_notification=True)
        else:
            await msg.reply("Duck status: no active duck.", disable_notification=True)

    @r.message(Command("duckstats"))
    async def duckstats(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        rows = await rt.duckhunt.leaderboard(msg.chat.id, limit=15)
        if not rows:
            await msg.reply("No duckhunt activity yet.", disable_notification=True)
            return
        lines = ["🦆 Duck Stats 🦆"]
        for i, row in enumerate(rows, 1):
            lines.append(
                f"{i}. {row['display_name']} — {row['points']} pts "
                f"(🔫 {row['killed']} 🤝 {row['befriended']} "
                f"⏭ {row['ignored']} ❌ {row['misses']}; "
                f"streak {row['streak']}/{row['best_streak']})"
            )
        await msg.reply("\n".join(lines), disable_notification=True)

    @r.message(Command("duckfriends"))
    async def duckfriends(msg: Message) -> None:
        """Show the calling user's roster of befriended ducks in this chat."""
        await get_or_create_chat_config(rt, msg)
        if not msg.from_user:
            return
        total = await rt.duckhunt.friend_count(msg.chat.id, msg.from_user.id)
        if not total:
            await msg.reply(
                "You haven't befriended any ducks here yet.",
                disable_notification=True,
            )
            return
        roster = await rt.duckhunt.friendship_roster(
            msg.chat.id, msg.from_user.id, limit=15,
        )
        lines = [f"🦆 Friends ({total}):"]
        for d in roster:
            lines.append(
                f"  duck #{d['id']} [{d['rarity']}] "
                f"— {d['resolved_at']:%Y-%m-%d %H:%M}"
            )
        await msg.reply("\n".join(lines), disable_notification=True)

    @r.message(F.text.lower().in_({"bang", "ignore"}))
    async def bang_or_ignore(msg: Message) -> None:
        cfg = await get_or_create_chat_config(rt, msg)
        if not cfg.duckhunt_enabled or not msg.from_user:
            return
        action = msg.text.strip().lower()
        if not await rt.duckhunt.cooldown_ok(
            msg.chat.id, msg.from_user.id,
            rt.settings.duckhunt_action_cooldown_seconds,
        ):
            await msg.reply("Cool it. Cooldown.", disable_notification=True)
            return

        if action == "bang":
            outcome, _ = await rt.duckhunt.handle_bang(
                chat_id=msg.chat.id, user_id=msg.from_user.id,
                display_name=display_name(msg.from_user),
            )
        else:
            outcome, _ = await rt.duckhunt.handle_ignore(
                chat_id=msg.chat.id, user_id=msg.from_user.id,
                display_name=display_name(msg.from_user),
            )
        if outcome is None:
            return  # no active duck
        await msg.reply(outcome.message, disable_notification=True)

    @r.message(F.text.lower() == "bef")
    async def bef_action(msg: Message) -> None:
        cfg = await get_or_create_chat_config(rt, msg)
        if not cfg.duckhunt_enabled or not msg.from_user:
            return

        # Block while a retry challenge is still outstanding.
        pending = await rt.duckhunt.get_bef_challenge(
            msg.chat.id, msg.from_user.id,
        )
        if pending:
            hint = f"Solve the challenge first (reply to the prompt above)."
            try:
                await msg.reply(hint, disable_notification=True)
            except Exception:
                pass
            return

        if not await rt.duckhunt.cooldown_ok(
            msg.chat.id, msg.from_user.id,
            rt.settings.duckhunt_action_cooldown_seconds,
        ):
            who = display_name(msg.from_user)
            issued = await _issue_bef_challenge(
                rt, msg, who,
                intro="Hang on — you just did something. Cool off a sec.",
            )
            if not issued:
                await msg.reply("Cool it. Cooldown.", disable_notification=True)
            return

        duck = await rt.duckhunt.active_duck(msg.chat.id)
        if not duck:
            return

        # Step 2 of the flow only runs if dice pass; but to keep ai_line
        # available for refusal flavor, we always ask the AI when the duck is
        # present and let the service decide. The service short-circuits on
        # dice fail (AI verdict is ignored in that branch).
        who = display_name(msg.from_user)
        friend_count = await rt.duckhunt.friend_count(
            msg.chat.id, msg.from_user.id,
        )
        ai_text = await rt.openai.chat(
            [
                {"role": "system", "content": DUCK_BEF_DECIDE_PROMPT.format(
                    display_name=who, rarity=duck.rarity, friend_count=friend_count,
                )},
                {"role": "user", "content": "bef"},
            ],
            max_tokens=120,
            temperature=1.0,
        )
        verdict, line = parse_verdict(ai_text, "ACCEPT", "REFUSE")
        log.info(
            "bef AI decision: chat=%s user=%s rarity=%s verdict=%s",
            msg.chat.id, msg.from_user.id, duck.rarity, verdict,
        )

        outcome, _ = await rt.duckhunt.handle_bef(
            chat_id=msg.chat.id, user_id=msg.from_user.id,
            display_name=who,
            ai_verdict=verdict,
            ai_line=line,
        )
        if outcome is None:
            return

        await msg.reply(outcome.message, disable_notification=True)

        # On refusal: post a challenge the user must solve before retrying.
        if not outcome.success:
            await _issue_bef_challenge(
                rt, msg, who,
                intro=(
                    "Looks like the duck doesn't want to be friends right "
                    "now. Try again later."
                ),
            )

    return r
