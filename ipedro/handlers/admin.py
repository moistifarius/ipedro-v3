"""Admin-only commands. All require private DM + admin user id."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from aiogram.exceptions import TelegramBadRequest

from ipedro.auth import is_admin_user
from ipedro.duckhunt.spawner import (
    build_quack_message, duckhunt_enabled_chat_ids, duckhunt_enabled_chats,
)
from ipedro.handlers.common import require_admin
from ipedro.kv import kv_delete, kv_get, kv_set
from ipedro.logging_setup import recent_log_lines
from ipedro.personas import (
    DEFAULT_PEDRO_PROMPT, current_pedro_prompt, set_pedro_prompt_override,
)
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)


def _chat_label(chat: dict) -> str:
    title = chat.get("title")
    if title:
        return f"{title} ({chat['type']})"[:60]
    return f"{chat['type']} {chat['chat_id']}"[:60]


def _chat_picker(chats: list[dict], action: str) -> InlineKeyboardMarkup | None:
    """Build a one-button-per-row picker. action is the callback prefix.

    Telegram caps callback_data at 64 bytes; the chat_id alone fits well
    inside that. We cap rows at 50 to keep the keyboard usable.
    """
    if not chats:
        return None
    rows = [
        [InlineKeyboardButton(
            text=_chat_label(c), callback_data=f"{action}:{c['chat_id']}",
        )]
        for c in chats[:50]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_router(rt: Runtime) -> Router:
    r = Router(name="admin")
    admin_ids = rt.settings.admin_ids

    async def _gate_callback(cb: CallbackQuery) -> bool:
        if not cb.from_user or not is_admin_user(cb.from_user.id, admin_ids):
            await cb.answer("Admin only.", show_alert=True)
            return False
        return True

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
        """Tail the bot's actual program logs.

        Usage: /logs [N] [filter]   e.g. /logs 50 spawner
        N defaults to 50, filter is a case-insensitive substring.
        """
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split(None, 2)
        limit = 50
        needle: str | None = None
        if len(parts) >= 2:
            try:
                limit = max(1, min(200, int(parts[1])))
            except ValueError:
                needle = parts[1]
        if len(parts) >= 3:
            needle = parts[2]
        lines = recent_log_lines(limit=limit, contains=needle)
        if not lines:
            await msg.reply(
                "No log lines (yet, or none match the filter).",
                disable_notification=True,
            )
            return
        body = "\n".join(lines)
        # Telegram caps a single message at 4096 chars; chunk and send.
        chunks: list[str] = []
        buf = ""
        for ln in lines:
            extra = ln + "\n"
            if len(buf) + len(extra) > 3800:
                chunks.append(buf)
                buf = extra
            else:
                buf += extra
        if buf:
            chunks.append(buf)
        for i, chunk in enumerate(chunks[:5], 1):
            header = f"-- logs ({i}/{len(chunks)}) --\n" if len(chunks) > 1 else ""
            await msg.reply(header + chunk, disable_notification=True)

    @r.message(Command("cmdlog"))
    async def cmdlog(msg: Message) -> None:
        """Command audit log (was previously /logs)."""
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

    @r.message(Command("quack_all"))
    async def quack_all(msg: Message) -> None:
        """Spawn a duck in every duckhunt-enabled chat that doesn't already have one."""
        if not await require_admin(msg, admin_ids):
            return
        chat_ids = await duckhunt_enabled_chat_ids(rt.db)
        if not chat_ids:
            await msg.reply(
                "No chats have duckhunt enabled.", disable_notification=True,
            )
            return
        spawned, skipped, failed = 0, 0, 0
        for chat_id in chat_ids:
            if await rt.duckhunt.active_duck(chat_id):
                skipped += 1
                continue
            try:
                duck = await rt.duckhunt.spawn_duck(
                    chat_id, rt.settings.duckhunt_duck_lifetime_seconds,
                )
                text = await build_quack_message(rt.openai, duck.rarity)
                await rt.bot.send_message(
                    chat_id, text, disable_notification=True,
                )
                spawned += 1
                log.info(
                    "quack_all: spawned in chat=%s rarity=%s event_id=%s",
                    chat_id, duck.rarity, duck.id,
                )
            except Exception as exc:
                failed += 1
                log.warning("quack_all failed for chat %s: %s", chat_id, exc)
        await msg.reply(
            f"Quacked in {spawned} chat(s). "
            f"Skipped {skipped} (duck already active). "
            f"Failed {failed}.",
            disable_notification=True,
        )

    @r.message(Command("quack_chat"))
    async def quack_chat(msg: Message) -> None:
        """Pick a duckhunt-enabled chat from a keyboard and spawn a duck there."""
        if not await require_admin(msg, admin_ids):
            return
        chats = await duckhunt_enabled_chats(rt.db)
        kb = _chat_picker(chats, "qchat")
        if kb is None:
            await msg.reply(
                "No chats have duckhunt enabled.", disable_notification=True,
            )
            return
        await msg.reply(
            "Pick a chat to spawn a duck in:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("qchat:"))
    async def on_quack_chat(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        try:
            target = int(cb.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Bad selection.", show_alert=True)
            return
        if await rt.duckhunt.active_duck(target):
            await cb.answer("That chat already has an active duck.", show_alert=True)
            return
        try:
            duck = await rt.duckhunt.spawn_duck(
                target, rt.settings.duckhunt_duck_lifetime_seconds,
            )
            text = await build_quack_message(rt.openai, duck.rarity)
            await rt.bot.send_message(target, text, disable_notification=True)
        except Exception as exc:
            log.warning("quack_chat failed for chat %s: %s", target, exc)
            await cb.answer(f"Failed: {exc}", show_alert=True)
            return
        log.info(
            "quack_chat: spawned in chat=%s rarity=%s event_id=%s",
            target, duck.rarity, duck.id,
        )
        if cb.message:
            try:
                await cb.message.edit_text(
                    f"🦆 Spawned in {target} (rarity: {duck.rarity}).",
                )
            except TelegramBadRequest:
                pass
        await cb.answer("Quacked.")

    @r.message(Command("facts_chat"))
    async def facts_chat(msg: Message) -> None:
        """Pick a chat from a keyboard and dump its durable facts."""
        if not await require_admin(msg, admin_ids):
            return
        chats = await rt.chats.list_known()
        kb = _chat_picker(chats, "fchat")
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat to show stored facts for:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("fchat:"))
    async def on_facts_chat(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        try:
            target = int(cb.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Bad selection.", show_alert=True)
            return
        facts = await rt.memory.list_facts(target, limit=50)
        if not facts:
            body = f"No facts stored for chat {target}."
        else:
            body = "\n".join(f"[{f.id}] {f.fact}" for f in facts)
        if cb.message:
            try:
                await cb.message.edit_text(body[:4000])
            except TelegramBadRequest:
                pass
        await cb.answer()

    @r.message(Command("pick_chat"))
    async def pick_chat(msg: Message) -> None:
        """Pick a chat from a keyboard; the bot replies with its id for copy-paste."""
        if not await require_admin(msg, admin_ids):
            return
        chats = await rt.chats.list_known()
        kb = _chat_picker(chats, "pchat")
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat to copy its id:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("pchat:"))
    async def on_pick_chat(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        try:
            target = int(cb.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Bad selection.", show_alert=True)
            return
        if cb.message:
            try:
                await cb.message.edit_text(
                    f"Chat id: <code>{target}</code>\n"
                    f"Use with: /send_message {target} your text",
                    parse_mode="HTML",
                )
            except TelegramBadRequest:
                pass
        await cb.answer(str(target))

    @r.message(Command("master_prompt"))
    async def master_prompt(msg: Message) -> None:
        """View / set / reset the global Pedro persona prompt."""
        if not await require_admin(msg, admin_ids):
            return
        raw = (msg.text or "").split(None, 2)
        sub = raw[1].lower() if len(raw) >= 2 else "show"
        if sub == "show":
            current = current_pedro_prompt()
            is_default = current == DEFAULT_PEDRO_PROMPT
            tag = "(default)" if is_default else "(override active)"
            await msg.reply(
                f"Master Pedro prompt {tag}:\n\n{current}",
                disable_notification=True,
            )
            return
        if sub == "reset":
            await kv_delete(rt.db, "pedro_master_prompt")
            set_pedro_prompt_override(None)
            await msg.reply(
                "Reset to default Pedro prompt.", disable_notification=True,
            )
            return
        if sub == "set":
            if len(raw) < 3 or not raw[2].strip():
                await msg.reply(
                    "Usage: /master_prompt set <new full prompt text>",
                    disable_notification=True,
                )
                return
            new_text = raw[2].strip()
            await kv_set(rt.db, "pedro_master_prompt", new_text)
            set_pedro_prompt_override(new_text)
            await msg.reply(
                f"Master prompt updated ({len(new_text)} chars).",
                disable_notification=True,
            )
            return
        await msg.reply(
            "Usage: /master_prompt show | set <text> | reset",
            disable_notification=True,
        )

    @r.message(Command("cost"))
    async def cost(msg: Message) -> None:
        """Show OpenAI spend (last 7 days). /cost or /cost <chat_id>."""
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split()
        chat_filter = None
        if len(parts) >= 2:
            try:
                chat_filter = int(parts[1])
            except ValueError:
                await msg.reply("Bad chat id.", disable_notification=True)
                return
        if chat_filter is not None:
            rows = await rt.db.fetch(
                "SELECT kind, COUNT(*) AS calls, "
                "       COALESCE(SUM(total_tokens), 0) AS tokens, "
                "       COALESCE(SUM(cost_usd), 0) AS cost "
                "  FROM openai_usage "
                " WHERE chat_id = $1 AND created_at >= NOW() - INTERVAL '7 days' "
                " GROUP BY kind ORDER BY cost DESC",
                chat_filter,
            )
            header = f"Last 7d for chat {chat_filter}:"
        else:
            rows = await rt.db.fetch(
                "SELECT kind, COUNT(*) AS calls, "
                "       COALESCE(SUM(total_tokens), 0) AS tokens, "
                "       COALESCE(SUM(cost_usd), 0) AS cost "
                "  FROM openai_usage "
                " WHERE created_at >= NOW() - INTERVAL '7 days' "
                " GROUP BY kind ORDER BY cost DESC"
            )
            header = "Last 7d (all chats):"
        if not rows:
            await msg.reply("No usage recorded in that window.", disable_notification=True)
            return
        lines = [header]
        total = 0.0
        for r in rows:
            c = float(r["cost"] or 0)
            total += c
            lines.append(
                f"  {r['kind']:<10}  {r['calls']:>5} calls  "
                f"{int(r['tokens']):>8} tokens  ${c:.4f}"
            )
        lines.append(f"  TOTAL: ${total:.4f}")
        await msg.reply("\n".join(lines), disable_notification=True)

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
