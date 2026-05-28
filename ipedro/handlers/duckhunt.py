"""Duckhunt slash commands and 'bang/bef/ignore' triggers."""

from __future__ import annotations

import logging
import random

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from ipedro.duckhunt.captcha_gen import make_captcha
from ipedro.duckhunt.debug_toggles import is_on as debug_is_on
from ipedro.duckhunt.spawner import build_quack_message_for
from ipedro.duckhunt.verdicts import parse_verdict
from ipedro.handlers.common import display_name, get_or_create_chat_config
from ipedro.prompts import (
    DUCK_BEF_CHALLENGE_PROMPT, DUCK_BEF_DECIDE_PROMPT,
)
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

_CHALLENGE_KINDS = ("captcha", "trivia", "recipe")

# Subset eligible for *random* selection on a bef refusal. Recipe is
# currently disabled here while we tune it; `/debug_recipe` still works
# because force_kind paths validate against the full _CHALLENGE_KINDS.
_RANDOM_CHALLENGE_KINDS: tuple[str, ...] = ("captcha", "trivia")

# A small pool of neutral celebration lines for a successful bef; one is
# picked at random so the bot doesn't sound like a stuck record. Rarity
# is currently neutralized — every duck gets the same flavor.
_BEF_FOLLOWUP_FLAIR: tuple[str, ...] = (
    "Welcome to the roster.",
    "Solid little quacker. Yours now.",
    "Just a duck. But yours.",
    "Friendship engaged.",
    "Another one for the collection.",
    "The duck nods. You nod. It's a moment.",
)


def _bef_celebration_message(duck_id: int, new_friend_total: int) -> str:
    """Compose the follow-up message sent after a successful bef.

    Random neutral flair + a milestone shout for round-number friend
    counts + a pre-filled /duckname hint with the duck id.
    """
    flair = random.choice(_BEF_FOLLOWUP_FLAIR)
    milestone = ""
    if new_friend_total == 1:
        milestone = " First feathered friend — somebody mark the calendar."
    elif new_friend_total in (5, 10, 25, 50):
        milestone = (
            f" That's your {new_friend_total}-duck club membership, official."
        )
    elif new_friend_total >= 100 and new_friend_total % 100 == 0:
        milestone = (
            f" {new_friend_total} ducks. You absolute duck whisperer."
        )
    elif new_friend_total % 10 == 0:
        milestone = f" {new_friend_total} and counting."
    return (
        f"🤝 You made a friend! {flair}{milestone}\n"
        f"Want to name them? Reply: /duckname {duck_id} <name>"
    )


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
    kind = (
        force_kind if force_kind in _CHALLENGE_KINDS
        else random.choice(_RANDOM_CHALLENGE_KINDS)
    )
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
            "Manual spawn in chat %s by user %s -> duck_id=%s",
            msg.chat.id, msg.from_user.id if msg.from_user else None, duck.id,
        )
        await msg.answer(
            await build_quack_message_for(rt.openai, duck),
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

    @r.message(Command("global_leaderboard"))
    async def global_leaderboard(msg: Message) -> None:
        rows = await rt.duckhunt.global_leaderboard(limit=15)
        if not rows:
            await msg.reply("No duckhunt activity yet anywhere.", disable_notification=True)
            return
        lines = ["🌐 Global Duck Leaderboard 🌐"]
        for i, row in enumerate(rows, 1):
            lines.append(
                f"{i}. {row['display_name']} — {row['points']} pts "
                f"(🔫 {row['killed']} 🤝 {row['befriended']}; "
                f"across {row['chats']} chats)"
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
            name_part = f" \"{d['name']}\"" if d.get("name") else ""
            lines.append(
                f"  duck #{d['id']}{name_part} "
                f"— {d['resolved_at']:%Y-%m-%d %H:%M}"
            )
        lines.append("\nTip: /duckname <id> <name> to name one.")
        await msg.reply("\n".join(lines), disable_notification=True)

    @r.message(Command("duckname"))
    async def duckname(msg: Message) -> None:
        """Name one of your befriended ducks: /duckname <duck_id> <name>."""
        if not msg.from_user:
            return
        parts = (msg.text or "").split(None, 2)
        if len(parts) < 3:
            await msg.reply(
                "Usage: /duckname <duck_id> <name>",
                disable_notification=True,
            )
            return
        try:
            duck_id = int(parts[1])
        except ValueError:
            await msg.reply("Bad duck id.", disable_notification=True)
            return
        name = parts[2].strip()[:60]
        ok = await rt.duckhunt.name_duck(
            msg.chat.id, msg.from_user.id, duck_id, name,
        )
        if ok:
            await msg.reply(
                f"🦆 Duck #{duck_id} is now \"{name}\".",
                disable_notification=True,
            )
        else:
            await msg.reply(
                "You haven't befriended that duck here.",
                disable_notification=True,
            )

    @r.message(F.text.lower().in_({"bang", "ignore"}))
    async def bang_or_ignore(msg: Message) -> None:
        cfg = await get_or_create_chat_config(rt, msg)
        if not cfg.duckhunt_enabled or not msg.from_user:
            return
        action = msg.text.strip().lower()
        admin_id = msg.from_user.id
        # bypass_cooldowns lets an admin re-bang/-bef rapidly while
        # testing. Non-admins (or admins with the toggle off) get the
        # normal 15s cooldown.
        if not debug_is_on(admin_id, "bypass_cooldowns") and not await rt.duckhunt.cooldown_ok(
            msg.chat.id, msg.from_user.id,
            rt.settings.duckhunt_action_cooldown_seconds,
        ):
            await msg.reply("Cool it. Cooldown.", disable_notification=True)
            return

        if action == "bang":
            # always_hit / always_miss short-circuit the dice roll inside
            # handle_bang. They're mutually exclusive — always_hit wins.
            forced: bool | None = None
            if debug_is_on(admin_id, "always_hit"):
                forced = True
            elif debug_is_on(admin_id, "always_miss"):
                forced = False
            outcome, _ = await rt.duckhunt.handle_bang(
                chat_id=msg.chat.id, user_id=msg.from_user.id,
                display_name=display_name(msg.from_user),
                forced_success=forced,
            )
        else:
            outcome, _ = await rt.duckhunt.handle_ignore(
                chat_id=msg.chat.id, user_id=msg.from_user.id,
                display_name=display_name(msg.from_user),
            )
        if outcome is None:
            # No active duck. Reply briefly instead of silent return so the
            # user gets feedback when a stale /quackflag misled them.
            await msg.reply(
                "🦆 No duck here. Wait for one to spawn.",
                disable_notification=True,
            )
            return
        await msg.reply(outcome.message, disable_notification=True)

    @r.message(F.text.lower() == "bef")
    async def bef_action(msg: Message) -> None:
        cfg = await get_or_create_chat_config(rt, msg)
        if not cfg.duckhunt_enabled or not msg.from_user:
            return
        admin_id = msg.from_user.id

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

        # bypass_cooldowns lets an admin re-bef while debugging.
        if not debug_is_on(admin_id, "bypass_cooldowns") and not await rt.duckhunt.cooldown_ok(
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
            await msg.reply(
                "🦆 No duck here. Wait for one to spawn.",
                disable_notification=True,
            )
            return

        # Step 2 of the flow only runs if dice pass; but to keep ai_line
        # available for refusal flavor, we always ask the AI when the duck is
        # present and let the service decide. The service short-circuits on
        # dice fail (AI verdict is ignored in that branch).
        who = display_name(msg.from_user)
        friend_count = await rt.duckhunt.friend_count(
            msg.chat.id, msg.from_user.id,
        )
        # always_refuse_bef short-circuits the AI call so the admin can
        # force the refusal -> challenge path for debugging.
        if debug_is_on(admin_id, "always_refuse_bef"):
            verdict: bool | None = False
            line = "The duck refuses. [debug: always_refuse_bef]"
            log.info(
                "bef debug-forced refusal: chat=%s user=%s",
                msg.chat.id, msg.from_user.id,
            )
        else:
            ai_text = await rt.openai.chat(
                [
                    {"role": "system", "content": DUCK_BEF_DECIDE_PROMPT.format(
                        display_name=who, friend_count=friend_count,
                    )},
                    {"role": "user", "content": "bef"},
                ],
                max_tokens=120,
                temperature=1.0,
            )
            verdict, line = parse_verdict(ai_text, "ACCEPT", "REFUSE")
            log.info(
                "bef AI decision: chat=%s user=%s verdict=%s",
                msg.chat.id, msg.from_user.id, verdict,
            )

        outcome, duck_after = await rt.duckhunt.handle_bef(
            chat_id=msg.chat.id, user_id=msg.from_user.id,
            display_name=who,
            ai_verdict=verdict,
            ai_line=line,
        )
        if outcome is None:
            return

        await msg.reply(outcome.message, disable_notification=True)

        # On success: follow up with a celebration + /duckname hint.
        if outcome.success and duck_after is not None:
            follow_up = _bef_celebration_message(
                duck_after.id, friend_count + 1,
            )
            try:
                await msg.answer(follow_up, disable_notification=True)
            except Exception as exc:
                log.debug("bef follow-up send failed: %s", exc)

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
