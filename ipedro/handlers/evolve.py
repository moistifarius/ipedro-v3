"""/evolve — the Telegram surface for the owner's change requests.

The flow and the trust boundary it's built around live in
ipedro/evolve.py. This module is the command, the File / Cancel buttons,
and the owner check on both: a callback is its own request, so the button
handler re-checks rather than trusting that only the owner could have
seen the button.
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from ipedro import evolve
from ipedro.auth import AuthContext, is_owner
from ipedro.handlers.common import require_owner
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

_STATUS_ICON = {
    "pending": "⏳", "filing": "📤", "filed": "✅",
    "failed": "⚠️", "cancelled": "✖️",
}

# Telegram's message cap is 4096; the stored request is never cut, only
# this preview of it.
_PREVIEW_LIMIT = 3000


def _keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ File it", callback_data=f"evo:{request_id}:file"),
        InlineKeyboardButton(text="Cancel", callback_data=f"evo:{request_id}:cancel"),
    ]])


async def _edit(cb: CallbackQuery, text: str) -> None:
    try:
        await cb.message.edit_text(text, disable_web_page_preview=True)
    except Exception as exc:  # message too old to edit, etc.
        log.info("evolve: couldn't edit the draft message: %s", exc)


async def _usage_and_recent(rt: Runtime) -> str:
    lines = [
        "Usage: <code>/evolve &lt;what you want changed&gt;</code>",
        "Your words go to the coding agent exactly as you type them, so say "
        "it the way you'd tell a developer. You'll see the issue before "
        "anything is filed.",
    ]
    try:
        await evolve.recover_stuck(rt.db)
    except Exception as exc:
        log.info("evolve: couldn't sweep stuck requests: %s", exc)
    rows = await evolve.recent_requests(rt.db, limit=8)
    if rows:
        lines.append("\nRecent requests:")
        for r in rows:
            snippet = r["request"].strip().splitlines()[0][:60]
            tail = f" — issue #{r['issue_number']}" if r.get("issue_number") else ""
            lines.append(
                f"{_STATUS_ICON.get(r['status'], '•')} #{r['id']} "
                f"{html.escape(snippet)}{tail}"
            )
    return "\n".join(lines)


def build_router(rt: Runtime) -> Router:
    r = Router(name="evolve")

    @r.message(Command("evolve"))
    async def evolve_cmd(msg: Message) -> None:
        if not await require_owner(msg, rt.settings.owner_id):
            return
        parts = (msg.text or "").split(None, 1)
        request = parts[1].strip() if len(parts) == 2 else ""
        if not request:
            await msg.reply(
                await _usage_and_recent(rt), parse_mode="HTML",
                disable_notification=True,
            )
            return
        if not rt.settings.evolve_github_token:
            await msg.reply(
                "Can't file anything yet: EVOLVE_GITHUB_TOKEN isn't set. It "
                "needs a fine-grained GitHub token for "
                f"{rt.settings.evolve_github_repo} with Issues read/write "
                "and nothing else.",
                disable_notification=True,
            )
            return
        request_id = await evolve.create_request(rt.db, msg.from_user.id, request)
        title, body = evolve.build_issue(request_id, request)
        preview = body if len(body) <= _PREVIEW_LIMIT else (
            body[:_PREVIEW_LIMIT] + "\n… (the full text gets filed)"
        )
        await msg.reply(
            f"<b>Request #{request_id}</b>: this is exactly what gets filed.\n\n"
            f"<b>{html.escape(title)}</b>\n<pre>{html.escape(preview)}</pre>",
            reply_markup=_keyboard(request_id),
            parse_mode="HTML",
            disable_notification=True,
        )
        await rt.command_log.add(
            msg.chat.id, msg.from_user.id, "/evolve", f"#{request_id} drafted", True,
        )

    @r.callback_query(F.data.startswith("evo:"))
    async def on_evolve(cb: CallbackQuery) -> None:
        if not cb.data or not cb.message:
            return
        ctx = AuthContext(
            user_id=cb.from_user.id if cb.from_user else None,
            chat_type=getattr(cb.message.chat, "type", "") or "",
        )
        if not is_owner(ctx, rt.settings.owner_id):
            await cb.answer("Owner only.", show_alert=True)
            return
        try:
            _, raw_id, action = cb.data.split(":", 2)
            request_id = int(raw_id)
        except ValueError:
            await cb.answer("Stale button.", show_alert=True)
            return

        if action == "cancel":
            if await evolve.cancel_request(rt.db, request_id):
                await cb.answer("Cancelled.")
                await _edit(cb, f"Request #{request_id} cancelled. Nothing was filed.")
                await rt.command_log.add(
                    cb.message.chat.id, ctx.user_id, "/evolve",
                    f"#{request_id} cancelled", True,
                )
            else:
                await cb.answer("Already handled.", show_alert=True)
            return
        if action != "file":
            await cb.answer("Stale button.", show_alert=True)
            return

        claimed = await evolve.claim_for_filing(rt.db, request_id)
        if claimed is None:
            await cb.answer("Already handled.", show_alert=True)
            return
        # From here the request is 'filing' and only mark_filed/mark_failed
        # moves it on, so nothing below may escape before one of them runs.
        # (Answering the tap used to sit unguarded here: a too-old callback
        # query raised, the handler died, and the request stayed 'filing'
        # for good with nothing filed.)
        try:
            await cb.answer("Filing…")
        except Exception as exc:
            log.info("evolve: couldn't answer the tap: %s", exc)
        token = rt.settings.evolve_github_token
        title, body = evolve.build_issue(request_id, claimed["request"])
        try:
            if not token:
                raise evolve.FilingError("EVOLVE_GITHUB_TOKEN isn't set any more")
            number, url = await evolve.file_issue(
                token=token, repo=rt.settings.evolve_github_repo,
                title=title, body=body,
            )
        except Exception as exc:
            reason = (
                str(exc) if isinstance(exc, evolve.FilingError)
                else f"unexpected {type(exc).__name__}"
            )
            if not isinstance(exc, evolve.FilingError):
                log.exception("evolve: filing request #%s failed", request_id)
            await evolve.mark_failed(rt.db, request_id, reason)
            await _edit(
                cb,
                f"Request #{request_id} didn't file: {reason}. "
                "Send /evolve again to retry.",
            )
            await rt.command_log.add(
                cb.message.chat.id, ctx.user_id, "/evolve",
                f"#{request_id} file", False, reason,
            )
            return
        try:
            await evolve.mark_filed(rt.db, request_id, number, url)
        except Exception:
            # The issue EXISTS. Say so, rather than let a bookkeeping error
            # hide the link (a retry would file it twice).
            log.exception("evolve: issue #%s filed but not recorded", number)
        await _edit(
            cb,
            f"Request #{request_id} filed as issue #{number}:\n{url}\n\n"
            "The build turns it into a pull request. A change to plain "
            "content (what I say) can merge itself once the tests pass; "
            "anything else waits for you.",
        )
        await rt.command_log.add(
            cb.message.chat.id, ctx.user_id, "/evolve",
            f"#{request_id} filed as issue #{number}", True,
        )

    return r
