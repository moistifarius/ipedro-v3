"""General-purpose user commands: /remind, /poll."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ipedro.reminders import add_reminder, parse_duration
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)


def build_router(rt: Runtime) -> Router:
    r = Router(name="utility")

    @r.message(Command("remind"))
    async def remind(msg: Message) -> None:
        """/remind <duration> <text> — e.g. /remind 1h30m feed the cats."""
        raw = msg.text or ""
        parts = raw.split(None, 2)
        if len(parts) < 3:
            await msg.reply(
                "Usage: /remind <duration> <text>\n"
                "Duration: 30s, 5m, 1h, 2h30m, 1d, 1w (any combo).",
                disable_notification=True,
            )
            return
        seconds = parse_duration(parts[1])
        if seconds is None:
            await msg.reply(
                "Couldn't parse the duration. Try things like '5m', '1h30m', '2d'.",
                disable_notification=True,
            )
            return
        body = parts[2].strip()
        if not body:
            await msg.reply("Empty reminder text.", disable_notification=True)
            return
        rid = await add_reminder(
            rt.db, msg.chat.id,
            msg.from_user.id if msg.from_user else None,
            body, seconds,
        )
        await msg.reply(
            f"⏰ Set. I'll remind you in {parts[1]} (#{rid}).",
            disable_notification=True,
        )

    @r.message(Command("poll"))
    async def poll(msg: Message) -> None:
        """/poll Question | Option A | Option B | ... (2-10 options)."""
        raw = (msg.text or "").split(None, 1)
        if len(raw) < 2:
            await msg.reply(
                "Usage: /poll Question | Option A | Option B | ...",
                disable_notification=True,
            )
            return
        parts = [p.strip() for p in raw[1].split("|") if p.strip()]
        if len(parts) < 3:
            await msg.reply(
                "Need a question and at least two options, separated by |.",
                disable_notification=True,
            )
            return
        question, options = parts[0], parts[1:11]  # Telegram caps at 10
        try:
            await rt.bot.send_poll(
                chat_id=msg.chat.id,
                question=question[:300],
                options=[o[:100] for o in options],
                is_anonymous=True,
            )
        except Exception as exc:
            await msg.reply(f"Poll failed: {exc}", disable_notification=True)

    return r
