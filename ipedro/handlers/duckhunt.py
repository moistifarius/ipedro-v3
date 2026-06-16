"""Duckhunt slash commands and 'bang/bef/ignore' triggers."""

from __future__ import annotations

import logging
import random
import time
from collections import deque

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from ipedro.bot_messages import track
from ipedro.duckhunt.captcha_gen import make_captcha
from ipedro.duckhunt.debug_toggles import is_on as debug_is_on
from ipedro.duckhunt.scoring import (
    challenge_time_limit_seconds, should_challenge_on_miss,
)
from ipedro.duckhunt.spawner import build_quack_message_for
from ipedro.duckhunt.verdicts import parse_verdict
from ipedro.handlers.common import display_name, get_or_create_chat_config
from ipedro.prompts import (
    DUCK_BEF_CHALLENGE_PROMPT, DUCK_BEF_DECIDE_PROMPT,
)
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

_CHALLENGE_KINDS = ("captcha", "trivia", "recipe")

# Subset eligible for *random* selection on a bef refusal / bang-miss
# spook. Recipe is currently disabled here while we tune it;
# `/debug_recipe` still works because force_kind paths validate against
# the full _CHALLENGE_KINDS. The weights bias toward captcha — it's the
# fastest to solve (no AI judge round-trip) and the user wanted them
# more frequent.
_RANDOM_CHALLENGE_KINDS: tuple[str, ...] = ("captcha", "trivia")
_RANDOM_CHALLENGE_WEIGHTS: tuple[int, ...] = (1, 3)  # captcha 25% / trivia 75%

# A small pool of neutral celebration lines for a successful bef; one is
# picked at random so the bot doesn't sound like a stuck record. Rarity
# is currently neutralized — every duck gets the same flavor.
_BEF_FOLLOWUP_FLAIR: tuple[str, ...] = (
    "Welcome to the network.",
    "Sh-sha. Friend confirmed.",
    "Code-named. Filed.",
    "Vetted. Add to the trusted list.",
    "Cleared. They're with us now.",
    "Approved. Eyes only.",
)


def _bef_celebration_message(duck_id: int, new_friend_total: int) -> str:
    """Compose the follow-up message sent after a successful bef.

    Random neutral flair + a milestone shout for round-number friend
    counts + an invitation to name the duck by *replying* to this
    message. The reply-handler is gated on the per-(chat, prompt_id)
    state registered by the bef handler when this message is sent.
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
        f"Want to name them? Reply to this message with the name."
    )


# Per-(chat, follow-up message id) state for the reply-to-name flow.
# When a bef succeeds the handler registers (chat_id, prompt_msg_id) →
# (user_id, duck_id, registered_at). A reply to that prompt by the same
# user, within the TTL, sets the duck's name. The legacy
# /duckname <id> <name> command continues to work alongside this.
_PENDING_NAMING: dict[tuple[int, int], tuple[int, int, float]] = {}
_NAMING_TTL_SECONDS = 30 * 60   # 30 min — long enough to AFK then come back


def _register_pending_name(
    chat_id: int, prompt_msg_id: int, user_id: int, duck_id: int,
) -> None:
    # Light TTL sweep so the dict doesn't grow forever in long-running bots.
    now = time.time()
    if len(_PENDING_NAMING) > 2000:
        for k, (_, _, ts) in list(_PENDING_NAMING.items()):
            if now - ts > _NAMING_TTL_SECONDS:
                _PENDING_NAMING.pop(k, None)
    _PENDING_NAMING[(chat_id, prompt_msg_id)] = (user_id, duck_id, now)


async def _is_naming_reply(msg: Message) -> bool:
    """aiogram filter — match only replies to a tracked naming prompt
    by the user who made the friend. Returning False lets the message
    fall through to other handlers (the catch-all chat router etc.)."""
    if msg.from_user is None or msg.reply_to_message is None:
        return False
    key = (msg.chat.id, msg.reply_to_message.message_id)
    entry = _PENDING_NAMING.get(key)
    if entry is None:
        return False
    user_id, _duck_id, ts = entry
    if time.time() - ts > _NAMING_TTL_SECONDS:
        _PENDING_NAMING.pop(key, None)
        return False
    if user_id != msg.from_user.id:
        return False
    text = (msg.text or msg.caption or "").strip()
    # Slash-commands are never names — let them route normally so the
    # user can still do /duckname etc. without us swallowing it.
    return bool(text) and not text.startswith("/")


# Said when someone types bang/bef/ignore and there's no active duck.
# The intent is mild gaslighting — make the trigger-happy user briefly
# question reality without crossing into hostile. Picked at random.
_NO_DUCK_FLAVOR: tuple[str, ...] = (
    "Sh-sha. Negative contact. No duck on the scope.",
    "There is no duck. There has never been a duck. Continue normal activity.",
    "What duck. Who told you there was a duck. Who's asking.",
    "Empty room. Could be a drill.",
    "Negative. Stand down.",
    "No duck. Could be a hologram you didn't see leave.",
    "Sh-sha! Shooting at a phantom. That's how they get you.",
    "Nothing here. Possible misdirection.",
    "Easy. Could be a decoy that already moved on.",
    "Sh-sha. Maintain situational awareness. No duck.",
    "Eyes peeled, but no — nothing here. Stand by.",
    "Pocket sand! ...sorry, reflex. No duck.",
)


def _no_duck_line() -> str:
    return random.choice(_NO_DUCK_FLAVOR)


# Said when a user tries to bang/bef again before the per-user cooldown
# expires. Same paranoid tone as the rest, just shorter.
_COOLDOWN_FLAVOR: tuple[str, ...] = (
    "Sh-sha. Slow down. They're watching the pattern.",
    "Easy. You're drawing attention.",
    "Stand down a sec.",
    "Sh-sha. Reload. Don't be predictable.",
    "Hold position. Too soon.",
    "Negative. Wait for the all-clear.",
    "Sh-sha. Pace yourself. Could be a setup.",
    "Eyes up. Not yet.",
    "Pocket sand! ...sorry. Wait a beat.",
)


def _cooldown_line() -> str:
    return random.choice(_COOLDOWN_FLAVOR)


# Per-chat ring buffer of recent trivia questions, so the AI doesn't
# loop the same "what is the only mammal that cannot jump" three days
# in a row. We send these into the prompt as an explicit avoid-list.
# In-memory only — fine for a chat-game gate. Cap at 12 entries/chat.
_RECENT_TRIVIA: dict[int, deque[str]] = {}
_RECENT_TRIVIA_PER_CHAT = 12


def _recent_trivia_for(chat_id: int) -> deque[str]:
    buf = _RECENT_TRIVIA.get(chat_id)
    if buf is None:
        buf = deque(maxlen=_RECENT_TRIVIA_PER_CHAT)
        _RECENT_TRIVIA[chat_id] = buf
    return buf


def _build_avoid_block(chat_id: int, kind: str) -> str:
    """Return the AVOID-list snippet to splice into DUCK_BEF_CHALLENGE_PROMPT.

    Only meaningful for trivia — captcha is image-rendered and recipe
    is single-shot. Returns an empty string for those so the prompt
    behavior is unchanged."""
    if kind != "trivia":
        return ""
    buf = _RECENT_TRIVIA.get(chat_id)
    if not buf:
        return ""
    listed = "\n".join(f"- {q}" for q in buf)
    return (
        "AVOID these recent questions in this chat — pick something "
        "from a completely different topic and angle:\n"
        f"{listed}"
    )


# Dale-flavored intros, framed as the DUCK testing the user — turns the
# challenge into part of the game lore instead of a generic gate. Per
# (action, kind) so bef-refusal and bang-miss spook read differently.
# The captcha image / trivia text carries the actual content.
_CAPTCHA_INTROS = {
    "bef": (
        "Duck wants ID. Read this back:",
        "Sh-sha. Verification required. What's it say:",
        "Duck doesn't trust you yet. Prove you're not a plant:",
        "Hold up. Could be a fed. Read the squiggle:",
        "Duck's running a background check. Type what you see:",
        "Identify yourself. Read the code:",
    ),
    "bang_miss": (
        "Sh-sha. The duck made you. Read this before you try again:",
        "Spooked it. Bird's onto you. Decode this:",
        "Whiffed. Could be a decoy. Verify:",
        "Sh-sha. Steady your hand. Read this first:",
        "Duck logged the miss. Sign in:",
    ),
}
_TRIVIA_INTROS = {
    "bef": (
        "Duck wants intel. Answer:",
        "Sh-sha. Security question first:",
        "Duck won't talk until you answer this:",
        "Knowledge check. Get it right:",
        "Duck's vetting you. Question:",
    ),
    "bang_miss": (
        "Sh-sha. Answer this before you reload:",
        "Duck's testing your training. Question:",
        "Steady. The duck has questions:",
    ),
}


def _challenge_intro(action: str, kind: str) -> str:
    """Pick a playful intro line for a (bef / bang_miss) × (captcha / trivia)
    combination. Falls back gracefully if a pool is missing."""
    pool_map = _CAPTCHA_INTROS if kind == "captcha" else _TRIVIA_INTROS
    pool = pool_map.get(action) or pool_map.get("bef") or ("Try again:",)
    return random.choice(pool)


# Game-show framings for trivia questions. One is picked at random per
# trivia challenge and spliced into the generation prompt so the duck's
# pop quiz reads like a real game show. Kept here (not in prompts.py) so
# the rotation is easy to tune from user feedback.
_TRIVIA_GAME_SHOW_STYLES: tuple[str, ...] = (
    "Family Feud — open with 'Survey says!' or 'We asked 100 people' and "
    "ask them to name something from a category. Several answers are "
    "acceptable; they only need to name one plausible one.",

    "Trivial Pursuit — a single-fact question with exactly one correct "
    "answer, like a trivia card. You may name a colored category wedge "
    "(history=brown, geography=blue, arts=pink, science=green, "
    "sports=orange, entertainment=yellow).",

    "Jeopardy — state the answer as a clue/fact and require the player to "
    "respond in the form of a question. You may give it a CATEGORY and a "
    "dollar value (e.g. 'WORLD CAPITALS for $400').",
)


def _trivia_style_block() -> str:
    """Pick a random game-show style and format it as a sub-bullet for
    DUCK_BEF_CHALLENGE_PROMPT. Only used for trivia kind."""
    style = random.choice(_TRIVIA_GAME_SHOW_STYLES)
    return f"  GAME-SHOW STYLE for this one — {style}\n"


def _clock_caption(kind: str) -> str:
    """The 'you're on the clock' line appended to a challenge message, so
    the player knows there's a time limit before they wander off to Google."""
    secs = challenge_time_limit_seconds(kind)
    if kind == "trivia":
        return f"⏱ {secs} seconds on the clock — no Googling, no lifelines."
    if kind == "recipe":
        return f"⏱ {secs} seconds. Cook fast."
    return f"⏱ {secs} seconds."


async def _issue_bef_challenge(
    rt: Runtime, msg: Message, who: str, *,
    from_action: str = "bef",
    force_kind: str | None = None,
) -> bool:
    """Generate a captcha/trivia/recipe challenge and store it as pending.

    Captcha is a real image captcha rendered server-side; the stored
    `challenge` text IS the answer (judged by exact match later). Trivia
    and recipe are AI-generated text challenges judged by an AI prompt.
    The intro is picked from a per-(action, kind) flavor pool so the
    bef-refusal challenge reads differently from a bang-miss spook
    without callers needing to compose anything.
    Returns True if the challenge was issued; False otherwise.
    """
    kind = (
        force_kind if force_kind in _CHALLENGE_KINDS
        else random.choices(
            _RANDOM_CHALLENGE_KINDS, weights=_RANDOM_CHALLENGE_WEIGHTS,
        )[0]
    )
    intro = _challenge_intro(from_action, kind)
    clock = _clock_caption(kind)
    if kind == "captcha":
        answer, png = make_captcha()
        try:
            prompt_msg = await msg.answer_photo(
                BufferedInputFile(png, filename="captcha.png"),
                caption=f"{intro}\n{clock}",
                disable_notification=True,
            )
        except Exception as exc:
            log.warning("Failed to send captcha to %s: %s", msg.chat.id, exc)
            return False
        await rt.duckhunt.set_bef_challenge(
            msg.chat.id, msg.from_user.id, answer, kind, prompt_msg.message_id,
        )
        return True

    # Quick challenge prompt — Haiku quality is plenty here. For trivia,
    # splice in the per-chat recent-trivia list so it picks a fresh
    # topic each time, plus a random game-show style framing.
    avoid_block = _build_avoid_block(msg.chat.id, kind)
    style_block = _trivia_style_block() if kind == "trivia" else ""
    challenge_text = await rt.openai.cheap_completion(
        DUCK_BEF_CHALLENGE_PROMPT.format(
            display_name=who, kind=kind,
            avoid_block=avoid_block, style_block=style_block,
        ),
        max_tokens=200,
    )
    if not challenge_text:
        log.info(
            "Skipping bef challenge for %s/%s - AI unavailable.",
            msg.chat.id, msg.from_user.id if msg.from_user else None,
        )
        return False
    if kind == "trivia":
        _recent_trivia_for(msg.chat.id).append(challenge_text)
    prompt_msg = await msg.answer(
        f"{intro}\n\n{challenge_text}\n\n{clock}",
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

    @r.message(Command("ducknames"))
    async def ducknames(msg: Message) -> None:
        """Show every named duck across every chat the bot's in.

        ``/ducknames``       → page 1 (newest 100 names)
        ``/ducknames <N>``   → page N

        Chats can opt out of being included in this global view by
        setting ``duck_names_public off`` in their chat config; their
        own /duckfriends and /duckstats still work normally.
        """
        page = 1
        parts = (msg.text or "").split()
        if len(parts) >= 2:
            try:
                page = max(1, int(parts[1]))
            except ValueError:
                pass
        per_page = 100
        offset = (page - 1) * per_page
        rows, total = await rt.duckhunt.list_named_ducks_global(
            limit=per_page, offset=offset,
        )
        if not rows and page == 1:
            await msg.reply(
                "No named ducks anywhere yet. (Set one with "
                "`/duckname <id> <name>` after a successful bef.)",
                disable_notification=True,
            )
            return
        if not rows:
            await msg.reply(
                f"Page {page} is past the end — {total} named ducks total.",
                disable_notification=True,
            )
            return
        last_pos = offset + len(rows)
        header = (
            f"🦆 Named ducks across the ether ({total} total, "
            f"showing {offset + 1}–{last_pos}):"
        )
        lines = [header]
        for d in rows:
            lines.append(f"  • {d['name']} — {d['owner']}")
        if last_pos < total:
            lines.append(f"\n+ {total - last_pos} more. "
                         f"Try `/ducknames {page + 1}`.")
        # Telegram caps outbound at 4096 chars; truncate defensively.
        await msg.reply("\n".join(lines)[:4000], disable_notification=True)

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

        # Pending challenge gate (matches bef): if the user has an
        # outstanding captcha / trivia / recipe challenge, both bang and
        # ignore are blocked until they clear it. Without the gate the
        # bang handler would fire before the chat-router challenge
        # intercept gets a look.
        if action == "bang":
            pending = await rt.duckhunt.get_bef_challenge(
                msg.chat.id, msg.from_user.id,
            )
            if pending:
                await msg.reply(
                    "Solve the challenge first (reply to the prompt above).",
                    disable_notification=True,
                )
                return

        # bypass_cooldowns lets an admin re-bang/-bef rapidly while
        # testing. Non-admins (or admins with the toggle off) get the
        # normal 15s cooldown.
        if not debug_is_on(admin_id, "bypass_cooldowns") and not await rt.duckhunt.cooldown_ok(
            msg.chat.id, msg.from_user.id,
            rt.settings.duckhunt_action_cooldown_seconds,
        ):
            await msg.reply(_cooldown_line(), disable_notification=True)
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
                _no_duck_line(),
                disable_notification=True,
            )
            return
        await msg.reply(outcome.message, disable_notification=True)

        # Missed bang → roll for a follow-up challenge. The challenge
        # gates further bangs (via the pending check above) until cleared.
        # Skipped for the admin always_miss toggle so debug runs stay
        # predictable, and for any bypass_cooldowns admin since the whole
        # point of that toggle is rapid-fire testing.
        if (
            action == "bang"
            and not outcome.success
            and not debug_is_on(admin_id, "always_miss")
            and not debug_is_on(admin_id, "always_hit")
            and not debug_is_on(admin_id, "bypass_cooldowns")
            and should_challenge_on_miss()
        ):
            await _issue_bef_challenge(
                rt, msg, display_name(msg.from_user),
                from_action="bang_miss",
            )

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
            # Cooldown branch — reuse the bef flavor pool, not the
            # bang_miss one (still a bef attempt the user fired).
            issued = await _issue_bef_challenge(rt, msg, who, from_action="bef")
            if not issued:
                await msg.reply(_cooldown_line(), disable_notification=True)
            return

        duck = await rt.duckhunt.active_duck(msg.chat.id)
        if not duck:
            await msg.reply(
                _no_duck_line(),
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
            # ACCEPT/REFUSE verdict — quick classification, Haiku is fine.
            ai_text = await rt.openai.cheap_chat(
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

        # On refusal we used to send the duck's "no thanks" line AND
        # then a challenge intro that said the same thing — two
        # near-identical lines back-to-back. The challenge intro now
        # carries the refusal flavor, so on a refusal skip the duplicate
        # outcome.message reply entirely. (Successes still echo so the
        # AI accept line lands.)
        if outcome.success:
            await msg.reply(outcome.message, disable_notification=True)

        # On success: follow up with a celebration. The user can then
        # reply to the follow-up with a name (reply-to-name handler) or
        # use /duckname N <name> the old way.
        if outcome.success and duck_after is not None:
            follow_up = _bef_celebration_message(
                duck_after.id, friend_count + 1,
            )
            try:
                sent = await msg.answer(follow_up, disable_notification=True)
                track(msg.chat.id, sent.message_id, follow_up)
                _register_pending_name(
                    msg.chat.id, sent.message_id,
                    msg.from_user.id, duck_after.id,
                )
            except Exception as exc:
                log.debug("bef follow-up send failed: %s", exc)

        # On refusal: post a challenge the user must solve before
        # retrying. The challenge's own intro carries the refusal flavor
        # (we suppressed outcome.message above to avoid the duplicate).
        if not outcome.success:
            await _issue_bef_challenge(rt, msg, who, from_action="bef")

    @r.message(_is_naming_reply)
    async def name_on_reply(msg: Message) -> None:
        """Reply to a 'Want to name them?' celebration → set the duck's name.

        Filter ensures we only see legitimate replies to a tracked,
        unexpired prompt from the user who befriended the duck — other
        replies (including challenge answers, which target a different
        prompt id) fall through to the normal routers."""
        if msg.from_user is None or msg.reply_to_message is None:
            return  # belt-and-suspenders (filter already covers this)
        key = (msg.chat.id, msg.reply_to_message.message_id)
        entry = _PENDING_NAMING.pop(key, None)
        if entry is None:
            return  # raced with another reply; safe to drop
        _user_id, duck_id, _ts = entry
        name = (msg.text or msg.caption or "").strip()[:60]
        ok = await rt.duckhunt.name_duck(
            msg.chat.id, msg.from_user.id, duck_id, name,
        )
        if ok:
            await msg.reply(
                f"🦆 Duck #{duck_id} is now \"{name}\".",
                disable_notification=True,
            )
        else:
            # Re-register so a follow-up retry still works.
            _register_pending_name(
                msg.chat.id, msg.reply_to_message.message_id,
                msg.from_user.id, duck_id,
            )
            await msg.reply(
                "Couldn't set that name. Try /duckname "
                f"{duck_id} <name>.",
                disable_notification=True,
            )

    return r
