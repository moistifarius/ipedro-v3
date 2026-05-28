"""Mod tools: /shutup, /unshutup, /snark_at, /unsnark, /flags."""

from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ipedro.auth import is_admin_user
from ipedro.handlers.common import display_name
from ipedro.reminders import parse_duration
from ipedro.runtime import Runtime
from ipedro.user_flags import (
    clear_flag, has_flag, list_flags, set_flag, VALID_FLAGS,
)

log = logging.getLogger(__name__)


async def _admin_or_chat_admin(rt: Runtime, msg: Message) -> bool:
    """Admin (global) OR chat admin/creator may use mod commands."""
    if not msg.from_user or not msg.chat:
        return False
    if is_admin_user(msg.from_user.id, rt.settings.admin_ids):
        return True
    try:
        member = await rt.bot.get_chat_member(msg.chat.id, msg.from_user.id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


async def _resolve_target_user(rt: Runtime, msg: Message) -> int | None:
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user.id
    parts = (msg.text or "").split()
    for tok in parts[1:]:
        if tok.startswith("@"):
            row = await rt.db.fetchrow(
                "SELECT user_id FROM users WHERE LOWER(username) = LOWER($1)",
                tok[1:],
            )
            if row:
                return row["user_id"]
    return None


def build_router(rt: Runtime) -> Router:
    r = Router(name="mod")

    async def _gated(msg: Message) -> bool:
        if not await _admin_or_chat_admin(rt, msg):
            await msg.reply(
                "Mod commands are admin / chat-admin only.",
                disable_notification=True,
            )
            return False
        return True

    @r.message(Command("shutup"))
    async def shutup(msg: Message) -> None:
        if not await _gated(msg):
            return
        target = await _resolve_target_user(rt, msg)
        if target is None:
            await msg.reply(
                "Usage: /shutup @user [duration]  (reply-to also works).",
                disable_notification=True,
            )
            return
        # Optional duration is the last non-@ arg.
        parts = (msg.text or "").split()
        ttl = None
        for tok in reversed(parts[1:]):
            if tok.startswith("@"):
                continue
            sec = parse_duration(tok)
            if sec is not None:
                ttl = timedelta(seconds=sec)
            break
        await set_flag(rt.db, msg.chat.id, target, "shutup", ttl=ttl)
        suffix = f" for {parts[-1]}" if ttl else " indefinitely"
        await msg.reply(
            f"🤐 Will ignore user {target}{suffix}.",
            disable_notification=True,
        )

    @r.message(Command("unshutup"))
    async def unshutup(msg: Message) -> None:
        if not await _gated(msg):
            return
        target = await _resolve_target_user(rt, msg)
        if target is None:
            await msg.reply(
                "Usage: /unshutup @user", disable_notification=True,
            )
            return
        ok = await clear_flag(rt.db, msg.chat.id, target, "shutup")
        await msg.reply(
            "Listening again." if ok else "Wasn't shutup.",
            disable_notification=True,
        )

    @r.message(Command("snark_at"))
    async def snark_at(msg: Message) -> None:
        if not await _gated(msg):
            return
        target = await _resolve_target_user(rt, msg)
        if target is None:
            await msg.reply(
                "Usage: /snark_at @user (reply-to also works).",
                disable_notification=True,
            )
            return
        await set_flag(rt.db, msg.chat.id, target, "snark")
        await msg.reply(
            f"😏 Snark dialed up for user {target}.",
            disable_notification=True,
        )

    @r.message(Command("unsnark"))
    async def unsnark(msg: Message) -> None:
        if not await _gated(msg):
            return
        target = await _resolve_target_user(rt, msg)
        if target is None:
            await msg.reply(
                "Usage: /unsnark @user", disable_notification=True,
            )
            return
        ok = await clear_flag(rt.db, msg.chat.id, target, "snark")
        await msg.reply(
            "Snark back to baseline." if ok else "Not on the snark list.",
            disable_notification=True,
        )

    @r.message(Command("ungrudge"))
    async def ungrudge(msg: Message) -> None:
        """Forgive an auto-grudge (admin / chat-admin)."""
        if not await _gated(msg):
            return
        target = await _resolve_target_user(rt, msg)
        if target is None:
            await msg.reply("Usage: /ungrudge @user", disable_notification=True)
            return
        ok = await clear_flag(rt.db, msg.chat.id, target, "grudge")
        await msg.reply(
            "Grudge dropped." if ok else "No grudge against them.",
            disable_notification=True,
        )

    @r.message(Command("flags"))
    async def flags(msg: Message) -> None:
        if not await _gated(msg):
            return
        rows = await list_flags(rt.db, msg.chat.id)
        if not rows:
            await msg.reply("No active flags.", disable_notification=True)
            return
        lines = []
        for row in rows:
            exp = row["expires_at"]
            exp_part = f" until {exp:%Y-%m-%d %H:%M}" if exp else ""
            note = f" ({row['note']})" if row["note"] else ""
            lines.append(
                f"user {row['user_id']}  flag={row['flag']}{exp_part}{note}"
            )
        await msg.reply("\n".join(lines)[:4000], disable_notification=True)

    return r
