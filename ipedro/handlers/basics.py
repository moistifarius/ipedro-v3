"""/start /help and other zero-effort commands."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ipedro.handlers.common import get_or_create_chat_config
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

HELP_TEXT = (
    "Hi! I'm iPedro V2. I chat, generate images, transcribe voice, run "
    "duckhunt, and remember stuff over time.\n\n"
    "Common commands:\n"
    "/start - say hi\n"
    "/help - this message\n"
    "/a <q> - quick AI answer (no memory)\n"
    "/askai <q> - alias for /a\n"
    "/aigen <prompt> - generate an image\n"
    "/aiedit <prompt> - edit a replied-to image\n"
    "/aivar - variation of a replied-to image\n"
    "/aitranslate - translate a replied-to voice note\n"
    "/catfact - dubious cat fact\n"
    "/beneficiality - score whether I'd butt in right now\n"
    "/duckhunt - spawn a duck in this chat (if duckhunt enabled)\n"
    "/duckstats - leaderboard for this chat\n"
    "/duckfriends - your roster of befriended ducks here\n"
    "/duckname <id> <name> - name one of your befriended ducks\n"
    "/quackflag - is there an active duck?\n"
    "/remind <duration> <text> - schedule a future reminder (e.g. 1h30m)\n"
    "/poll Q | A | B | ... - create a poll\n"
    "/get_chat_id - show this chat's id\n"
    "/chat_config - show or change response policy/persona\n"
    "\nReply to ducks with: bang, bef, ignore.\n"
    "Bef may be refused - the duck decides. If refused, you'll get a small "
    "challenge to solve (reply to it) before you can try bef again.\n"
    "Say 'bad bot' or 'bad pedro' as a reply to my message to delete it."
)


def build_router(rt: Runtime) -> Router:
    r = Router(name="basics")

    @r.message(Command("start"))
    async def start(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        await msg.reply("Hello! I am iPedro V2. Type /help for commands.")
        await rt.command_log.add(
            msg.chat.id if msg.chat else None,
            msg.from_user.id if msg.from_user else None,
            "/start", None, True,
        )

    @r.message(Command("help"))
    async def help_(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        await msg.reply(HELP_TEXT, disable_notification=True)

    @r.message(Command("get_chat_id"))
    async def get_chat_id(msg: Message) -> None:
        chat_id = msg.chat.id if msg.chat else None
        await msg.reply(f"Current chat id: {chat_id}", disable_notification=True)

    return r
