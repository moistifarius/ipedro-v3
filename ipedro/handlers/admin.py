"""Admin-only commands. All require private DM + admin user id."""

from __future__ import annotations

import io
import logging
import time

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
from ipedro.memory.summarizer import force_summarize
from ipedro.memory.tokens import count_tokens
from ipedro.personas import (
    DEFAULT_DUDE_PROMPT, current_master_prompt, set_master_prompt_override,
)
from ipedro.runtime import Runtime

# Admin-keyed stash for /memory_search: the user types `/memory_search <query>`
# (the query can't fit in a 64-byte callback_data alongside the chat id), so
# we park it here keyed by the admin's user_id and read it back when they
# click a chat in the picker.
_PENDING_SEARCH_QUERIES: dict[int, tuple[str, float]] = {}
_SEARCH_QUERY_TTL = 300.0  # seconds; safety in case the admin never clicks


def _stash_search_query(user_id: int, query: str) -> None:
    _PENDING_SEARCH_QUERIES[user_id] = (query, time.time())


def _pop_search_query(user_id: int) -> str | None:
    entry = _PENDING_SEARCH_QUERIES.pop(user_id, None)
    if entry is None:
        return None
    query, ts = entry
    if time.time() - ts > _SEARCH_QUERY_TTL:
        return None
    return query


# Upper bound on the byte size of an uploaded /master_prompt file. Anything
# bigger than this is almost certainly a mistake — the in-context budget
# tops out at a few thousand tokens (settings.context_max_tokens), so 64 KB
# of raw text already overshoots usable territory. We accept up to this and
# then warn separately if the token count exceeds the runtime budget.
_MASTER_PROMPT_FILE_MAX_BYTES = 64 * 1024

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

    @r.message(Command("ai_provider"))
    async def ai_provider(msg: Message) -> None:
        """Switch the text-completion provider. /ai_provider show|claude|openai."""
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split()
        if len(parts) < 2 or parts[1].lower() == "show":
            await msg.reply(
                f"Current text provider: {rt.openai.text_provider}\n"
                f"  claude model: {rt.openai.claude_model}\n"
                f"  openai model: {rt.openai.text_model}\n"
                "Switch with: /ai_provider claude  or  /ai_provider openai",
                disable_notification=True,
            )
            return
        choice = parts[1].lower()
        if choice not in ("claude", "openai"):
            await msg.reply(
                "Usage: /ai_provider show | claude | openai",
                disable_notification=True,
            )
            return
        try:
            rt.openai.set_text_provider(choice)
        except ValueError as exc:
            await msg.reply(f"Can't switch: {exc}", disable_notification=True)
            return
        await kv_set(rt.db, "text_provider", choice)
        await msg.reply(
            f"Text provider is now {choice}.",
            disable_notification=True,
        )

    @r.message(Command("ai_model"))
    async def ai_model(msg: Message) -> None:
        """Switch the text model used by the active provider.

        /ai_model show
        /ai_model <model_id>          # picks the right slot for the active provider
        /ai_model claude <model_id>   # explicitly set the Claude model
        /ai_model openai <model_id>   # explicitly set the OpenAI model
        """
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split()
        if len(parts) < 2 or parts[1].lower() == "show":
            await msg.reply(
                f"Provider: {rt.openai.text_provider}\n"
                f"  claude model: {rt.openai.claude_model}\n"
                f"  openai model: {rt.openai.text_model}",
                disable_notification=True,
            )
            return
        if len(parts) >= 3 and parts[1].lower() in ("claude", "openai"):
            slot = parts[1].lower()
            new_model = parts[2]
        else:
            slot = rt.openai.text_provider
            new_model = parts[1]
        if slot == "claude":
            rt.openai.set_claude_model(new_model)
            await kv_set(rt.db, "claude_text_model", new_model)
        else:
            rt.openai.set_openai_text_model(new_model)
            await kv_set(rt.db, "openai_text_model", new_model)
        await msg.reply(
            f"{slot} text model is now {new_model}.",
            disable_notification=True,
        )

    async def _store_master_prompt(msg: Message, new_text: str) -> None:
        """Persist a new master prompt and surface a token-budget warning
        if it's large enough that build_context() would refuse to include it."""
        new_text = new_text.strip()
        await kv_set(rt.db, "master_prompt", new_text)
        set_master_prompt_override(new_text)
        tokens = count_tokens(new_text)
        budget = rt.settings.context_max_tokens
        suffix = ""
        if tokens >= budget:
            suffix = (
                f"\n\n⚠️ {tokens} tokens exceeds context_max_tokens ({budget}); "
                "the persona will be dropped at runtime. Shorten the prompt "
                "or raise CONTEXT_MAX_TOKENS."
            )
        elif tokens > budget * 0.75:
            suffix = (
                f"\n\n⚠️ {tokens} tokens uses >75% of the {budget}-token "
                "context budget; little room left for memory/history."
            )
        await msg.reply(
            f"Master prompt updated ({len(new_text)} chars, "
            f"~{tokens} tokens).{suffix}",
            disable_notification=True,
        )

    @r.message(Command("master_prompt"))
    async def master_prompt(msg: Message) -> None:
        """View / set / reset the global master persona prompt."""
        if not await require_admin(msg, admin_ids):
            return
        # Command may arrive as msg.text or as msg.caption (when a document
        # is uploaded with the command as its caption).
        raw = (msg.text or msg.caption or "").split(None, 2)
        sub = raw[1].lower() if len(raw) >= 2 else "show"
        if sub == "show":
            current = current_master_prompt()
            is_default = current == DEFAULT_DUDE_PROMPT
            tag = "(default Dude)" if is_default else "(override active)"
            tokens = count_tokens(current)
            head = (
                f"Master persona prompt {tag} "
                f"({len(current)} chars, ~{tokens} tokens):\n\n"
            )
            # Keep the whole reply under Telegram's 4096-char outbound limit.
            await msg.reply(
                head + current[: 4000 - len(head)],
                disable_notification=True,
            )
            return
        if sub == "reset":
            await kv_delete(rt.db, "master_prompt")
            await kv_delete(rt.db, "pedro_master_prompt")  # legacy key
            set_master_prompt_override(None)
            await msg.reply(
                "Reset to default Dude prompt.", disable_notification=True,
            )
            return
        if sub == "set":
            if len(raw) < 3 or not raw[2].strip():
                await msg.reply(
                    "Usage: /master_prompt set <new full prompt text>\n"
                    "Or attach a .txt file and reply to it with "
                    "/master_prompt setfile (for prompts longer than 4079 "
                    "chars).",
                    disable_notification=True,
                )
                return
            await _store_master_prompt(msg, raw[2])
            return
        if sub == "setfile":
            # Accept the document either attached to this message (as caption)
            # or in a message we're replying to.
            doc = msg.document or (
                msg.reply_to_message.document if msg.reply_to_message else None
            )
            if doc is None:
                await msg.reply(
                    "Send the new prompt as a .txt file and either caption "
                    "it `/master_prompt setfile` or reply to the file with "
                    "`/master_prompt setfile`.",
                    disable_notification=True,
                )
                return
            if doc.file_size and doc.file_size > _MASTER_PROMPT_FILE_MAX_BYTES:
                await msg.reply(
                    f"That file is {doc.file_size} bytes; cap is "
                    f"{_MASTER_PROMPT_FILE_MAX_BYTES}. Shorten the prompt.",
                    disable_notification=True,
                )
                return
            try:
                file = await msg.bot.get_file(doc.file_id)
                buf = io.BytesIO()
                await msg.bot.download_file(file.file_path, destination=buf)
            except Exception as exc:
                log.warning("master_prompt setfile download failed: %s", exc)
                await msg.reply(
                    f"Couldn't download that file: {exc}",
                    disable_notification=True,
                )
                return
            data = buf.getvalue()
            if len(data) > _MASTER_PROMPT_FILE_MAX_BYTES:
                await msg.reply(
                    f"File is {len(data)} bytes; cap is "
                    f"{_MASTER_PROMPT_FILE_MAX_BYTES}.",
                    disable_notification=True,
                )
                return
            try:
                new_text = data.decode("utf-8")
            except UnicodeDecodeError:
                await msg.reply(
                    "File isn't valid UTF-8. Save it as plain UTF-8 text "
                    "and try again.",
                    disable_notification=True,
                )
                return
            if not new_text.strip():
                await msg.reply(
                    "File is empty — refusing to clobber the prompt.",
                    disable_notification=True,
                )
                return
            await _store_master_prompt(msg, new_text)
            return
        await msg.reply(
            "Usage: /master_prompt show | set <text> | setfile | reset",
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

    async def _send_facts_for(target: int, reply_to: Message | None,
                              edit_in: Message | None) -> None:
        """Format and send the fact list for one chat.

        Either replies to `reply_to` (typed-arg path) or edits `edit_in`
        in place (callback path).
        """
        facts = await rt.memory.list_facts(target, limit=50)
        if not facts:
            body = f"No facts stored for chat {target}."
        else:
            head = f"Facts for chat {target} ({len(facts)}):\n"
            body = head + "\n".join(f"[{f.id}] {f.fact}" for f in facts)
        body = body[:4000]
        if reply_to:
            await reply_to.reply(body, disable_notification=True)
        elif edit_in:
            try:
                await edit_in.edit_text(body)
            except TelegramBadRequest:
                pass

    @r.message(Command("memory_facts"))
    async def memory_facts(msg: Message) -> None:
        """Inspect durable facts. /memory_facts opens a chat picker;
        /memory_facts <chat_id> jumps straight to that chat."""
        if not await require_admin(msg, admin_ids):
            return
        args = (msg.text or "").split()
        if len(args) >= 2:
            try:
                target = int(args[1])
            except ValueError:
                await msg.reply("Invalid chat id.", disable_notification=True)
                return
            await _send_facts_for(target, reply_to=msg, edit_in=None)
            return
        chats = await rt.chats.list_known()
        kb = _chat_picker(chats, "mfacts")
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat to show stored facts for:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("mfacts:"))
    async def on_memory_facts(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        try:
            target = int(cb.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Bad selection.", show_alert=True)
            return
        await _send_facts_for(target, reply_to=None, edit_in=cb.message)
        await cb.answer()

    @r.message(Command("memory_facts_all"))
    async def memory_facts_all(msg: Message) -> None:
        """Dump every fact across every known chat, grouped by chat."""
        if not await require_admin(msg, admin_ids):
            return
        chats = await rt.chats.list_known()
        if not chats:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        sections: list[str] = []
        total_facts = 0
        chats_with_facts = 0
        for c in chats:
            facts = await rt.memory.list_facts(c["chat_id"], limit=100)
            if not facts:
                continue
            chats_with_facts += 1
            total_facts += len(facts)
            head = f"━ {_chat_label(c)} [{c['chat_id']}] ({len(facts)}):"
            body = "\n".join(f"  [{f.id}] {f.fact}" for f in facts)
            sections.append(f"{head}\n{body}")
        if not sections:
            await msg.reply(
                "No facts stored in any known chat yet.",
                disable_notification=True,
            )
            return
        footer = (
            f"\n\nTotal: {total_facts} facts across "
            f"{chats_with_facts} chat(s)."
        )
        # Telegram caps outbound at 4096; chunk sections so each reply fits.
        chunks: list[str] = []
        current = ""
        for section in sections:
            piece = ("\n\n" if current else "") + section
            if len(current) + len(piece) > 3900:
                chunks.append(current)
                current = section
            else:
                current += piece
        if current:
            chunks.append(current)
        # Footer attaches to the last chunk if it fits, else gets its own.
        if chunks and len(chunks[-1]) + len(footer) <= 3900:
            chunks[-1] += footer
        else:
            chunks.append(footer.lstrip())
        for chunk in chunks:
            await msg.reply(chunk, disable_notification=True)

    @r.message(Command("memory_stats"))
    async def memory_stats(msg: Message) -> None:
        """Picker → per-chat memory diagnostics: counts, freshness,
        embedding coverage, summary state."""
        if not await require_admin(msg, admin_ids):
            return
        chats = await rt.chats.list_known()
        kb = _chat_picker(chats, "mstats")
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat for memory stats:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("mstats:"))
    async def on_memory_stats(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        try:
            target = int(cb.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Bad selection.", show_alert=True)
            return
        # Per-chat aggregates via direct SQL (admin-only diagnostic, not a
        # hot path; cheaper than adding a half-dozen one-off repo methods).
        msg_rows = await rt.db.fetch(
            "SELECT role, COUNT(*) AS n, MIN(created_at) AS oldest, "
            "MAX(created_at) AS newest FROM messages "
            "WHERE chat_id = $1 GROUP BY role",
            target,
        )
        emb_rows = await rt.db.fetch(
            "SELECT ref_kind, COUNT(*) AS n FROM embeddings "
            "WHERE chat_id = $1 GROUP BY ref_kind",
            target,
        )
        fact_count = await rt.db.fetchval(
            "SELECT COUNT(*) FROM facts WHERE chat_id = $1", target,
        )
        summary_row = await rt.db.fetchrow(
            "SELECT COUNT(*) AS n, MAX(created_at) AS newest "
            "FROM summaries WHERE chat_id = $1",
            target,
        )
        latest_summary = await rt.memory.latest_summary(target)
        # Embedding coverage % for messages.
        msg_total = sum(r["n"] for r in msg_rows) or 0
        msg_embed_count = next(
            (r["n"] for r in emb_rows if r["ref_kind"] == "message"), 0,
        )
        coverage = (msg_embed_count / msg_total * 100) if msg_total else 0
        roles_line = ", ".join(
            f"{r['role']}={r['n']}" for r in msg_rows
        ) or "(none)"
        oldest = min(
            (r["oldest"] for r in msg_rows if r["oldest"]), default=None,
        )
        newest = max(
            (r["newest"] for r in msg_rows if r["newest"]), default=None,
        )
        emb_breakdown = ", ".join(
            f"{r['ref_kind']}={r['n']}" for r in emb_rows
        ) or "(none)"
        next_summary_at = rt.settings.summary_trigger_messages
        since_last_summary = 0
        if latest_summary:
            since_last_summary = await rt.memory.messages.count_since(
                target, latest_summary.covers_until_id,
            )
        else:
            since_last_summary = msg_total
        lines = [
            f"📊 Memory stats — chat {target}",
            "",
            f"Messages: {msg_total} ({roles_line})",
            f"  oldest: {oldest}",
            f"  newest: {newest}",
            "",
            f"Facts: {fact_count}",
            f"Summaries: {summary_row['n']} (latest at {summary_row['newest']})",
            f"  next auto-summarize in: "
            f"{max(0, next_summary_at - since_last_summary)} message(s)"
            f"  (have {since_last_summary} since last)",
            "",
            f"Embeddings: {emb_breakdown}",
            f"  message embedding coverage: {coverage:.1f}% "
            f"({msg_embed_count}/{msg_total})",
            f"  pgvector available: {rt.memory.pgvector_available}",
            f"  embedding model: {rt.openai.embedding_model} "
            f"(dim={rt.openai.embedding_dim})",
        ]
        body = "\n".join(lines)
        if cb.message:
            try:
                await cb.message.edit_text(body[:4000])
            except TelegramBadRequest:
                pass
        await cb.answer()

    @r.message(Command("memory_summary"))
    async def memory_summary(msg: Message) -> None:
        """Picker → show the latest stored summary for the chat."""
        if not await require_admin(msg, admin_ids):
            return
        chats = await rt.chats.list_known()
        kb = _chat_picker(chats, "msum")
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat to show its latest summary:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("msum:"))
    async def on_memory_summary(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        try:
            target = int(cb.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Bad selection.", show_alert=True)
            return
        summary = await rt.memory.latest_summary(target)
        if summary is None:
            body = f"No summary stored for chat {target} yet."
        else:
            head = (
                f"Latest summary for chat {target}\n"
                f"id={summary.id} covers_until={summary.covers_until_id} "
                f"at {summary.created_at}\n\n"
            )
            body = head + summary.summary
        if cb.message:
            try:
                await cb.message.edit_text(body[:4000])
            except TelegramBadRequest:
                pass
        await cb.answer()

    @r.message(Command("memory_summarize_now"))
    async def memory_summarize_now(msg: Message) -> None:
        """Picker → force a summarization + fact-extraction pass on a chat,
        ignoring the message-count threshold."""
        if not await require_admin(msg, admin_ids):
            return
        chats = await rt.chats.list_known()
        kb = _chat_picker(chats, "mforce")
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat to force-summarize:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("mforce:"))
    async def on_memory_summarize_now(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        try:
            target = int(cb.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Bad selection.", show_alert=True)
            return
        await cb.answer("Running summarizer…")
        try:
            report = await force_summarize(
                rt.memory, rt.openai, rt.settings, target,
            )
        except Exception as exc:
            log.warning("force_summarize failed for chat %s: %s", target, exc)
            if cb.message:
                try:
                    await cb.message.edit_text(
                        f"Summarizer failed: {exc}",
                    )
                except TelegramBadRequest:
                    pass
            return
        if not report.get("ok"):
            body = (
                f"Skipped chat {target}: {report.get('reason', 'unknown')}"
            )
        else:
            facts_added = report.get("facts_added", [])
            facts_block = (
                "\n".join(f"  - {f}" for f in facts_added)
                if facts_added else "  (none)"
            )
            body = (
                f"Force-summarized chat {target}.\n"
                f"  messages summarized: {report['messages_summarized']}\n"
                f"  summary id: {report['summary_id']} "
                f"({report['summary_chars']} chars)\n"
                f"  facts added ({len(facts_added)}):\n{facts_block}"
            )
        if cb.message:
            try:
                await cb.message.edit_text(body[:4000])
            except TelegramBadRequest:
                pass

    @r.message(Command("memory_search"))
    async def memory_search(msg: Message) -> None:
        """Semantic-search the embedding store.

        /memory_search <query>             → picker, then search the chosen chat
        /memory_search <chat_id> <query>   → search that chat directly
        """
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split(None, 2)
        if len(parts) < 2:
            await msg.reply(
                "Usage: /memory_search <query>\n"
                "   or: /memory_search <chat_id> <query>",
                disable_notification=True,
            )
            return
        # Try the two-arg form first (chat_id then query).
        target: int | None = None
        query: str
        if len(parts) >= 3:
            try:
                target = int(parts[1])
                query = parts[2].strip()
            except ValueError:
                # `parts[1]` isn't an int → treat whole tail as the query.
                query = (parts[1] + " " + parts[2]).strip()
        else:
            query = parts[1].strip()
        if not query:
            await msg.reply("Empty query.", disable_notification=True)
            return
        if target is not None:
            await _run_memory_search(msg, target, query)
            return
        # No chat_id → stash the query and let the admin pick a chat.
        if msg.from_user is None:
            await msg.reply("Can't identify caller.", disable_notification=True)
            return
        _stash_search_query(msg.from_user.id, query)
        chats = await rt.chats.list_known()
        kb = _chat_picker(chats, "msrch")
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            f"Search query stashed ({len(query)} chars). "
            "Pick a chat to search:",
            reply_markup=kb,
            disable_notification=True,
        )

    async def _run_memory_search(target_msg: Message, chat_id: int,
                                 query: str) -> None:
        hits = await rt.memory.semantic_search(chat_id, query, k=10)
        if not hits:
            await target_msg.reply(
                f"No semantic hits in chat {chat_id} for "
                f"{query!r} (pgvector available: "
                f"{rt.memory.pgvector_available}).",
                disable_notification=True,
            )
            return
        head = (
            f"🔎 Top {len(hits)} hits in chat {chat_id} for {query!r}:\n"
        )
        body_lines = []
        for h in hits:
            sim = h.get("similarity", 0)
            kind = h.get("ref_kind", "?")
            ref_id = h.get("ref_id", "?")
            content = (h.get("content") or "").replace("\n", " ")
            if len(content) > 220:
                content = content[:220] + "…"
            body_lines.append(
                f"  [{sim:.3f}] ({kind} #{ref_id}) {content}"
            )
        await target_msg.reply(
            (head + "\n".join(body_lines))[:4000],
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("msrch:"))
    async def on_memory_search(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        try:
            target = int(cb.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Bad selection.", show_alert=True)
            return
        if cb.from_user is None:
            await cb.answer("Can't identify caller.", show_alert=True)
            return
        query = _pop_search_query(cb.from_user.id)
        if query is None:
            await cb.answer(
                "Search query expired — re-run /memory_search.",
                show_alert=True,
            )
            return
        await cb.answer("Searching…")
        # Replace the picker with a placeholder while we work.
        if cb.message:
            try:
                await cb.message.edit_text(
                    f"Searching chat {target} for {query!r}…",
                )
            except TelegramBadRequest:
                pass
            await _run_memory_search(cb.message, target, query)

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

    # ---------------------------------------------------------------- duckstats reset
    async def _render_chat_user_resetter(target: int) -> tuple[str, InlineKeyboardMarkup | None]:
        """Build the leaderboard-style picker used to choose a user (or
        'all') to reset within one chat. Returns (text, keyboard)."""
        rows = await rt.db.fetch(
            "SELECT user_id, display_name, points, killed, befriended, misses "
            "  FROM duck_stats "
            " WHERE chat_id = $1 "
            " ORDER BY points DESC, killed DESC "
            " LIMIT 20",
            target,
        )
        if not rows:
            return (
                f"No duck stats stored for chat {target}.",
                None,
            )
        kb_rows: list[list[InlineKeyboardButton]] = []
        for r in rows:
            label = (
                f"{(r['display_name'] or str(r['user_id']))[:30]} — "
                f"{r['points']}pts ({r['killed']}🔫 {r['befriended']}🤝)"
            )[:60]
            kb_rows.append([InlineKeyboardButton(
                text=label,
                callback_data=f"dsru:{target}:{r['user_id']}",
            )])
        kb_rows.append([InlineKeyboardButton(
            text=f"⚠️  Reset ALL {len(rows)} users in this chat",
            callback_data=f"dsra:{target}",
        )])
        head = (
            f"Top {len(rows)} duckhunters in chat {target}.\n"
            "Tap one to reset their stats, or 'Reset ALL' to clear the whole "
            "chat's leaderboard.\n\n"
            "(Friendships and named ducks live in duck_events and are NOT "
            "touched by this — only the duck_stats counters are cleared.)"
        )
        return head, InlineKeyboardMarkup(inline_keyboard=kb_rows)

    async def _do_reset_user_stats(target: int, user_id: int) -> int:
        """Delete the duck_stats row. Returns 1 on hit, 0 on miss."""
        status = await rt.db.execute(
            "DELETE FROM duck_stats WHERE chat_id = $1 AND user_id = $2",
            target, user_id,
        )
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def _do_reset_all_in_chat(target: int) -> int:
        status = await rt.db.execute(
            "DELETE FROM duck_stats WHERE chat_id = $1", target,
        )
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError):
            return 0

    @r.message(Command("duckstats_reset"))
    async def duckstats_reset(msg: Message) -> None:
        """Reset duckhunt counters (admin-only).

        /duckstats_reset                       → chat picker → user picker
        /duckstats_reset <chat_id>             → user picker for that chat
        /duckstats_reset <chat_id> <user_id>   → direct reset of one user
        /duckstats_reset <chat_id> all         → wipe the whole chat's stats
        """
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split()
        if len(parts) >= 3:
            try:
                chat_id = int(parts[1])
            except ValueError:
                await msg.reply("Invalid chat id.", disable_notification=True)
                return
            if parts[2].lower() == "all":
                n = await _do_reset_all_in_chat(chat_id)
                await msg.reply(
                    f"Wiped duck_stats for chat {chat_id} ({n} row(s) deleted).",
                    disable_notification=True,
                )
                return
            try:
                user_id = int(parts[2])
            except ValueError:
                await msg.reply(
                    "Invalid user id (use a numeric Telegram user id, or "
                    "'all' to wipe the whole chat).",
                    disable_notification=True,
                )
                return
            n = await _do_reset_user_stats(chat_id, user_id)
            await msg.reply(
                f"Reset duck_stats for user {user_id} in chat {chat_id} "
                f"({n} row(s) deleted).",
                disable_notification=True,
            )
            return
        if len(parts) == 2:
            try:
                chat_id = int(parts[1])
            except ValueError:
                await msg.reply("Invalid chat id.", disable_notification=True)
                return
            text, kb = await _render_chat_user_resetter(chat_id)
            await msg.reply(
                text, reply_markup=kb, disable_notification=True,
            )
            return
        # No args → chat picker first.
        chats = await rt.chats.list_known()
        kb = _chat_picker(chats, "dsr")
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat to manage duckhunt stats for:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("dsr:"))
    async def on_dsr_chat_picked(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        try:
            target = int(cb.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Bad selection.", show_alert=True)
            return
        text, kb = await _render_chat_user_resetter(target)
        if cb.message:
            try:
                await cb.message.edit_text(text, reply_markup=kb)
            except TelegramBadRequest:
                pass
        await cb.answer()

    @r.callback_query(F.data.startswith("dsru:"))
    async def on_dsr_user_picked(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        try:
            _, chat_id_s, user_id_s = cb.data.split(":", 2)
            chat_id = int(chat_id_s)
            user_id = int(user_id_s)
        except (ValueError, IndexError):
            await cb.answer("Bad selection.", show_alert=True)
            return
        n = await _do_reset_user_stats(chat_id, user_id)
        body = (
            f"Reset duck_stats for user {user_id} in chat {chat_id} "
            f"({n} row(s) deleted)."
        )
        if cb.message:
            try:
                await cb.message.edit_text(body)
            except TelegramBadRequest:
                pass
        await cb.answer("Stats reset.")

    @r.callback_query(F.data.startswith("dsra:"))
    async def on_dsr_reset_all(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        try:
            target = int(cb.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Bad selection.", show_alert=True)
            return
        n = await _do_reset_all_in_chat(target)
        body = (
            f"Wiped duck_stats for chat {target} ({n} row(s) deleted)."
        )
        if cb.message:
            try:
                await cb.message.edit_text(body)
            except TelegramBadRequest:
                pass
        await cb.answer("Chat wiped.")

    return r
