"""/start /help and other zero-effort commands."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from ipedro.auth import is_admin_user
from ipedro.db.chat_migration import migrate_chat
from ipedro.handlers.common import get_or_create_chat_config
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

HELP_TEXT_PUBLIC = (
    "Sh-sha. Name's Dale. Also Rusty Shackleford, when I'm being watched. "
    "I chat, generate images, transcribe voice, hunt ducks, and remember "
    "stuff — more or less.\n"
    "\n"
    "Basics:\n"
    "/start, /help, /get_chat_id\n"
    "/chat_config [field value] - show/change this chat's settings\n"
    "/config - inline-keyboard settings wizard\n"
    "  (📟 ether: lets this chat receive /ether radio transmissions from "
    "other ether-enabled chats. Off by default; turn it on if you want to "
    "be reachable.)\n"
    "\n"
    "AI:\n"
    "/a, /askai, /ask <q> - quick AI answer (no memory write)\n"
    "/aigen, /generate <prompt> - generate an image\n"
    "/aiedit <prompt> - edit a replied-to image (placeholder)\n"
    "/aivar - variation of a replied-to image (placeholder)\n"
    "/aitranslate - translate a replied-to voice note\n"
    "/catfact - dubious cat fact\n"
    "/beneficiality - score whether I'd butt in right now\n"
    "/whatdid @user - summarize what they've been up to\n"
    "/tldr [duration] - summarize the recent chat (default 24h)\n"
    "/mood - my current mood + word of the day\n"
    "/haiku - haiku about the recent chat\n"
    "/this_or_that A | B - I decide, dramatically\n"
    "/echo @user [topic] - I mimic their style\n"
    "  (or just say 'act like <name>' / 'do a <name> impression' in chat — "
    "I'll reply in that member's voice, learned from their history)\n"
    "/roast @user, /compliment @user\n"
    "/fixname <wrong> -> <right> - fix a name I keep getting wrong "
    "(recursively across my notes; never touches what you actually typed)\n"
    "/lyric <line> - I mishear it\n"
    "/meme top text | bottom text - generate a meme image\n"
    "/redditmeme [topic] (/rmeme) - post from r/popular, or search a meme "
    "about a topic, + its top comment (or just say 'gimme a meme about "
    "this' in chat)\n"
    "/ether <text> - transmit a message as a staticky far-away radio "
    "voice into another ether-tuned chat (or send/reply /ether on a "
    "voice note to transmit the real recording)\n"
    "\n"
    "Duckhunt:\n"
    "/duckhunt - spawn a duck (if enabled)\n"
    "/quackflag - is there an active duck?\n"
    "/duckstats - leaderboard for this chat\n"
    "/duckfriends - your roster of befriended ducks here\n"
    "/duckname <id> <name> - name one of your befriended ducks\n"
    "/ducknames [page] - every named duck across every chat (paginated)\n"
    "/global_leaderboard - duck leaderboard across all chats\n"
    "\n"
    "Personality tests:\n"
    "/tests (/quiz) - menu of personality tests, tap to take one\n"
    "/disgusttest (/icktest) - how easily grossed out you are (food + macabre)\n"
    "/darktriad (/villain) - how much of a bastard you are\n"
    "/foodtest (/neophobia) - picky vs adventurous eater\n"
    "/bigfive (/personality) - your OCEAN personality profile\n"
    "  (each: a picture per question, tap the scale, scored profile + result "
    "card + Retake; based on validated, cited scales; for fun, not diagnoses)\n"
    "/disgustboard, /darktriadboard, /foodboard - per-test leaderboards\n"
    "\n"
    "Quotes & lore:\n"
    "/quote - reply to save, or call alone for a random quote\n"
    "/quotes - list recent quotes\n"
    "/unquote <id> - delete a saved quote\n"
    "/catchphrases @user - their repeated phrases\n"
    "/lexicon @user - their top words\n"
    "/heatmap - this chat's activity by hour-of-day\n"
    "/onthisday - what people said on this day in past months/years\n"
    "/whoslurking - users silent for >7 days\n"
    "/karma - chat karma leaderboard (👍/👎 reactions grant/dock)\n"
    "\n"
    "Reminders & dates:\n"
    "/remind <duration> <text> - e.g. /remind 1h30m feed the cat\n"
    "/birthday MM-DD - set your birthday\n"
    "/anniversary <name> MM-DD-YYYY - mark a chat date\n"
    "/dates - list all tracked dates here\n"
    "/poll Q | A | B | ... - create a poll\n"
    "\n"
    "DM only:\n"
    "/confess <text> - submit an anonymous confession\n"
    "\n"
    "Mod (chat admin or bot admin):\n"
    "/shutup @user [duration] - I ignore them\n"
    "/unshutup @user - release\n"
    "/snark_at @user - extra snark toward them\n"
    "/unsnark @user, /ungrudge @user - release\n"
    "/flags - active flags in this chat\n"
    "\n"
    "Ambient: reply to ducks with bang, bef, ignore. Bef may be refused — "
    "the duck decides. If refused, you'll get a small challenge to solve "
    "before you can try bef again. Say 'bad bot' or 'bad dale' as a reply "
    "to one of my messages to delete it."
)

HELP_TEXT_ADMIN = (
    "Bot admin (DM only):\n"
    "/manage - one-screen menu of every admin function (categorized)\n"
    "/list_chat_ids - list all known chats\n"
    "/pick_chat - picker that returns a chat id\n"
    "/config_for [chat_id] - open the /config wizard for any chat from DM\n"
    "/send_message <chat_id> <text> - send a message as the bot\n"
    "/delete_msg [chat_id] - picker → pick a recent bot message to delete\n"
    "/delete_last [chat_id] [N] - delete the last N (≤ 20) bot messages in a chat\n"
    "/silent_chat <chat_id>, /unsilent_chat <chat_id>, /silenced_chats - "
    "admin-only override: chats in the silenced set get "
    "disable_notification=True for celebration / fortune / retro / "
    "confession / ether sends. Not exposed in /chat_config.\n"
    "/logs [N] [filter] - tail the bot's program logs\n"
    "/cmdlog - command audit log from the DB\n"
    "/cost [chat_id] - AI spend (last 7 days)\n"
    "\n"
    "Persona & providers:\n"
    "/master_prompt show|set <text>|setfile|reset - global persona prompt\n"
    "/ai_provider show|claude|openai - switch text-completion provider\n"
    "/ai_model show|[provider] <model_id> - switch text model\n"
    "\n"
    "Ducks (admin spawning + stats):\n"
    "/quack_chat - picker → spawn a duck in that chat\n"
    "/quack_all - spawn a duck in every duckhunt-enabled chat\n"
    "/duckstats_reset - picker → wipe one user's (or all users') "
    "duck_stats counters in a chat (accepts @username)\n"
    "/duckstats_edit - picker → user → field/+/- editor for one user's stats\n"
    "\n"
    "Memory / embeddings:\n"
    "/memory_facts [chat_id] - picker (or direct) of stored facts\n"
    "/memory_facts_all - every fact across every chat\n"
    "/memory_forget <fact_id> - delete a fact\n"
    "/memory_wipe [chat_id] [facts] - erase a chat's conversation memory "
    "(messages + summary + embeddings) so a new persona stops getting "
    "dragged back to the old voice; add 'facts' to clear durable facts too\n"
    "/memory_stats - per-chat memory diagnostics (picker)\n"
    "/memory_summary - latest stored summary (picker)\n"
    "/memory_summarize_now - force a summary+fact-extraction pass (picker)\n"
    "/memory_search [chat_id] <query> - semantic search the embedding store\n"
    "/facts_chat - legacy alias for /memory_facts picker\n"
    "\n"
    "Debug:\n"
    "/debug_help - debug-command index\n"
    "/debug_captcha, /debug_challenge, /debug_trivia, /debug_recipe, "
    "/debug_duck, /debug_sharephoto, /debug_ether\n"
    "/debug_clear_challenge [chat_id] - clear stuck bef challenge(s) in a "
    "chat (unsticks endless 'Not quite. Try again.')\n"
    "/debug_toggle [name] [on|off] - admin-scoped duckhunt cheats "
    "(always_hit, always_miss, always_pass_challenge, "
    "always_fail_challenge, always_refuse_bef, bypass_cooldowns)\n"
    "/debug_clear_duck - picker → force-resolve a chat's active duck"
)


def build_router(rt: Runtime) -> Router:
    r = Router(name="basics")

    @r.message(F.migrate_to_chat_id.is_not(None))
    async def on_chat_migration(msg: Message) -> None:
        """Telegram upgraded this group to a supergroup and gave it a new id.
        Move all chat-scoped data across so stats/config/memory don't vanish.
        Registered first (this router loads before the catch-all) so the
        service message is handled here, not swallowed by chat handling."""
        old_id = msg.chat.id
        new_id = msg.migrate_to_chat_id
        if new_id is None or new_id == old_id:
            return
        try:
            moved = await migrate_chat(rt.db, old_id, new_id)
            log.info(
                "Supergroup migration: re-keyed chat %s -> %s (%s)",
                old_id, new_id,
                ", ".join(f"{t}:{n}" for t, n in moved.items()) or "nothing",
            )
        except Exception:
            log.exception(
                "Supergroup migration re-key failed %s -> %s", old_id, new_id,
            )

    @r.message(Command("start"))
    async def start(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        await msg.reply(
            "Sh-sha. Dale Gribble. Pest control. Conspiracy buff. "
            "Also goes by Rusty Shackleford. Type /help for commands."
        )
        await rt.command_log.add(
            msg.chat.id if msg.chat else None,
            msg.from_user.id if msg.from_user else None,
            "/start", None, True,
        )

    @r.message(Command("help"))
    async def help_(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        await msg.reply(HELP_TEXT_PUBLIC, disable_notification=True)
        # Append the admin reference as a second message when the caller
        # is a bot admin; non-admins don't see it at all.
        is_admin = (
            msg.from_user is not None
            and is_admin_user(msg.from_user.id, rt.settings.admin_ids)
        )
        if is_admin:
            await msg.reply(HELP_TEXT_ADMIN, disable_notification=True)

    @r.message(Command("get_chat_id"))
    async def get_chat_id(msg: Message) -> None:
        chat_id = msg.chat.id if msg.chat else None
        await msg.reply(f"Current chat id: {chat_id}", disable_notification=True)

    return r
