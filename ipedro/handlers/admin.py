"""Admin-only commands. All require private DM + admin user id."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest

from ipedro.handlers.common import require_admin
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)


def build_router(rt: Runtime) -> Router:
    r = Router(name="admin")
    admin_ids = rt.settings.admin_ids

    @r.message(Command("list_chat_ids"))
    async def list_chat_ids(msg: Message) -> None:
        if not await require_admin(msg, admin_ids):
            return
        rows = await rt.chats.list_known()
        if not rows:
            await msg.reply("No known chats yet.")
            return
        lines = [
            f"{r['chat_id']:>15} {r['type']:<10} {r['title'] or ''}"
            for r in rows
        ]
        text = "Known chats (last seen first):\n" + "\n".join(lines)
        # Telegram limits message length to 4096 chars.
        await msg.reply(text[:4000], disable_notification=True)

    @r.message(Command("send_message"))
    async def send_message_cmd(msg: Message) -> None:
        if not await require_admin(msg, admin_ids):
            return
        args = (msg.text or "").split(" ", 2)
        if len(args) < 3:
            await msg.reply(
                "Usage: /send_message <chat_id> <text>",
                disable_notification=True,
            )
            return
        try:
            target = int(args[1])
        except ValueError:
            await msg.reply("Invalid chat id.", disable_notification=True)
            return
        body = args[2]
        try:
            await rt.bot.send_message(target, body, disable_notification=True)
            await msg.reply("Sent.", disable_notification=True)
        except TelegramBadRequest as exc:
            await msg.reply(f"Send failed: {exc.message}", disable_notification=True)

    @r.message(Command("logs"))
    async def logs_cmd(msg: Message) -> None:
        if not await require_admin(msg, admin_ids):
            return
        rows = await rt.command_log.tail(30)
        if not rows:
            await msg.reply("No command log entries yet.")
            return
        lines = [
            f"{r['created_at']:%H:%M:%S} chat={r['chat_id']} user={r['user_id']}"
            f" {r['command']} ok={r['success']}"
            + (f" err={r['error']}" if r["error"] else "")
            for r in rows
        ]
        await msg.reply("Recent commands:\n" + "\n".join(lines), disable_notification=True)

    @r.message(Command("memory_facts"))
    async def memory_facts(msg: Message) -> None:
        """Inspect durable facts for a chat. Admin-only."""
        if not await require_admin(msg, admin_ids):
            return
        args = (msg.text or "").split()
        if len(args) < 2:
            await msg.reply("Usage: /memory_facts <chat_id>", disable_notification=True)
            return
        try:
            target = int(args[1])
        except ValueError:
            await msg.reply("Invalid chat id.", disable_notification=True)
            return
        facts = await rt.memory.list_facts(target, limit=50)
        if not facts:
            await msg.reply("No facts stored for that chat.", disable_notification=True)
            return
        out = "\n".join(f"[{f.id}] {f.fact}" for f in facts)
        await msg.reply(out[:4000], disable_notification=True)

    @r.message(Command("memory_forget"))
    async def memory_forget(msg: Message) -> None:
        """Delete a single durable fact by id. Admin-only."""
        if not await require_admin(msg, admin_ids):
            return
        args = (msg.text or "").split()
        if len(args) < 2:
            await msg.reply("Usage: /memory_forget <fact_id>", disable_notification=True)
            return
        try:
            fid = int(args[1])
        except ValueError:
            await msg.reply("Invalid fact id.", disable_notification=True)
            return
        await rt.memory.delete_fact(fid)
        await msg.reply(f"Deleted fact {fid}.", disable_notification=True)

    return r
