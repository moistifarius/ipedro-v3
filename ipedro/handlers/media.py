"""/pic and /pics — the pictures this chat has posted, on demand.

The natural-language path ("dale send that pic of the grill") lives in
handlers/chat.py next to the other intercepts; these are the explicit
commands for when you'd rather not phrase it.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ipedro import media_library
from ipedro.handlers.common import get_or_create_chat_config, require_memory
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)


def build_router(rt: Runtime) -> Router:
    r = Router(name="media")

    @r.message(Command("pic"))
    async def pic(msg: Message) -> None:
        """/pic <what> — send back the saved picture that best matches."""
        if not await require_memory(rt, msg):
            return
        cfg = await get_or_create_chat_config(rt, msg)
        parts = (msg.text or "").split(None, 1)
        query = parts[1].strip() if len(parts) > 1 else ""
        if not query:
            await msg.reply("Usage: /pic <what it showed, who posted it, when>",
                            disable_notification=True)
            return
        if await media_library.recall(rt, msg, cfg, query):
            return
        await msg.reply("Nothing saved here matches that.", disable_notification=True)

    @r.message(Command("pics"))
    async def pics(msg: Message) -> None:
        """/pics — the last few pictures posted here, as I remember them."""
        if not await require_memory(rt, msg):
            return
        await get_or_create_chat_config(rt, msg)
        rows = await media_library.recent(rt, msg.chat.id, limit=8)
        if not rows:
            await msg.reply("No pictures saved here yet.", disable_notification=True)
            return
        lines = ["Recent pictures I've got filed:"]
        for i, row in enumerate(rows, 1):
            who = row.get("posted_by_name") or "someone"
            lines.append(
                f"{i}. {who}, {media_library._ago(row.get('created_at'))}: "
                f"{(row.get('description') or '')[:120]}"
            )
        lines.append("Ask for one with /pic <what>, or just tell me to send it.")
        await msg.reply("\n".join(lines), disable_notification=True)

    return r
