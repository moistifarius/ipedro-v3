"""Karma tracker driven by native Telegram message reactions.

Each positive reaction (👍❤🔥🎉🥰👏🤝💯) on a tracked message gives that
message's author +1; each negative reaction (👎🤮💩😢🤡🤬) gives -1. Net
delta between the previous and new reaction set is applied. Bot messages
have no author in our messages table, so they accrue no karma.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, MessageReactionUpdated

from ipedro.handlers.common import display_name, get_or_create_chat_config
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

_POSITIVE = frozenset((
    "👍", "❤", "🔥", "🎉", "🥰", "👏", "🤝", "💯", "🤩",
    "😍", "🙏", "🏆", "👌", "❤‍🔥",
))
_NEGATIVE = frozenset((
    "👎", "🤮", "💩", "😢", "🤡", "🤬", "🖕", "💔",
))


def _score_set(reactions: list) -> int:
    score = 0
    for r in reactions or []:
        emoji = getattr(r, "emoji", None) or getattr(r, "custom_emoji_id", None)
        if emoji in _POSITIVE:
            score += 1
        elif emoji in _NEGATIVE:
            score -= 1
    return score


def build_router(rt: Runtime) -> Router:
    r = Router(name="karma")

    @r.message_reaction()
    async def on_reaction(event: MessageReactionUpdated) -> None:
        if not event.chat or not event.message_id:
            return
        delta = _score_set(event.new_reaction) - _score_set(event.old_reaction)
        if delta == 0:
            return
        # Author = whoever wrote the message we're reacting to.
        row = await rt.db.fetchrow(
            "SELECT m.user_id, u.first_name, u.username "
            "  FROM messages m "
            "  LEFT JOIN users u ON u.user_id = m.user_id "
            " WHERE m.chat_id = $1 AND m.message_id = $2 "
            "   AND m.user_id IS NOT NULL "
            " LIMIT 1",
            event.chat.id, event.message_id,
        )
        if not row:
            return
        author_id = row["user_id"]
        # Don't let a user self-karma.
        if event.user and event.user.id == author_id:
            return
        name = row["first_name"] or row["username"] or f"user{author_id}"
        await rt.db.execute(
            """
            INSERT INTO karma (chat_id, user_id, display_name, score)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (chat_id, user_id) DO UPDATE SET
                score = karma.score + EXCLUDED.score,
                display_name = EXCLUDED.display_name,
                updated_at = NOW()
            """,
            event.chat.id, author_id, name, delta,
        )

    @r.message(Command("karma"))
    async def karma_cmd(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        # /karma alone → leaderboard; /karma @user → that user's score.
        parts = (msg.text or "").split()
        if len(parts) >= 2 and parts[1].startswith("@"):
            uname = parts[1][1:]
            row = await rt.db.fetchrow(
                "SELECT k.score, k.display_name FROM karma k "
                "JOIN users u ON u.user_id = k.user_id "
                "WHERE k.chat_id = $1 AND LOWER(u.username) = LOWER($2)",
                msg.chat.id, uname,
            )
            if not row:
                await msg.reply(f"{uname} has 0 karma.", disable_notification=True)
                return
            await msg.reply(
                f"{row['display_name']}: {row['score']} karma.",
                disable_notification=True,
            )
            return
        rows = await rt.db.fetch(
            "SELECT display_name, score FROM karma "
            "WHERE chat_id = $1 ORDER BY score DESC LIMIT 15",
            msg.chat.id,
        )
        if not rows:
            await msg.reply(
                "No karma yet. React to messages with 👍 / 👎 / etc.",
                disable_notification=True,
            )
            return
        lines = ["⭐ Karma leaderboard:"]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['display_name']}: {row['score']}")
        await msg.reply("\n".join(lines), disable_notification=True)

    return r
