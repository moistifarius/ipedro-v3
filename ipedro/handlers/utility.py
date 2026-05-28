"""General-purpose user commands: /remind, /poll, /whatdid, /mood."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ipedro.handlers.common import display_name, get_or_create_chat_config
from ipedro.reminders import add_reminder, parse_duration
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

_WHATDID_PROMPT = (
    "Generate a confident, slightly exaggerated 2-3 sentence summary of "
    "what {name} has been up to in this chat lately, based ONLY on the "
    "messages below. Be playful, slightly suspicious, and present small "
    "things as if they were big deals. Do NOT quote the messages directly; "
    "synthesize. If there is nothing in the messages, invent a single "
    "absurd theory about what they've been up to elsewhere.\n\n"
    "Messages from {name}:\n{messages}"
)


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

    @r.message(Command("whatdid"))
    async def whatdid(msg: Message) -> None:
        """Confidently summarize what a user has been up to."""
        await get_or_create_chat_config(rt, msg)
        target_user_id: int | None = None
        target_name = "they"
        # Reply-to wins; else parse @username from the arg.
        if msg.reply_to_message and msg.reply_to_message.from_user:
            u = msg.reply_to_message.from_user
            target_user_id = u.id
            target_name = display_name(u)
        else:
            parts = (msg.text or "").split(None, 1)
            if len(parts) >= 2:
                arg = parts[1].strip().lstrip("@")
                row = await rt.db.fetchrow(
                    "SELECT user_id, first_name, last_name, username "
                    "  FROM users WHERE LOWER(username) = LOWER($1) LIMIT 1",
                    arg,
                )
                if row:
                    target_user_id = row["user_id"]
                    target_name = (
                        f"{row['first_name'] or ''} {row['last_name'] or ''}"
                    ).strip() or row["username"] or arg
        if target_user_id is None:
            await msg.reply(
                "Usage: /whatdid @username  (or reply to someone with /whatdid).",
                disable_notification=True,
            )
            return

        rows = await rt.db.fetch(
            "SELECT content FROM messages "
            " WHERE chat_id = $1 AND user_id = $2 "
            " ORDER BY id DESC LIMIT 30",
            msg.chat.id, target_user_id,
        )
        joined = "\n".join(f"- {r['content']}" for r in reversed(rows)) or "(none)"

        await rt.bot.send_chat_action(msg.chat.id, "typing")
        out = await rt.openai.short_completion(
            _WHATDID_PROMPT.format(name=target_name, messages=joined),
            max_tokens=200,
        )
        await msg.reply(
            out or f"No idea what {target_name} has been doing.",
            disable_notification=True,
        )

    @r.message(Command("mood"))
    async def mood(msg: Message) -> None:
        """Show this chat's current persona state (mood, word-of-the-day, stuck word)."""
        await get_or_create_chat_config(rt, msg)
        state = await rt.persona_state.current(msg.chat.id)
        lines = [f"Mood: {state.mood or 'unset'}"]
        if state.word_of_day:
            lines.append(f"Word of the day: {state.word_of_day}")
        if state.stuck_word:
            lines.append(f"Currently stuck on: {state.stuck_word}")
        await msg.reply("\n".join(lines), disable_notification=True)

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
