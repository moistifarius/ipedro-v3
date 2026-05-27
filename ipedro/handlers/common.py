"""Helpers shared across handler modules."""

from __future__ import annotations

import logging
from typing import Iterable

from aiogram.types import Message

from ipedro.auth import AuthContext, is_admin
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)


def auth_ctx(message: Message) -> AuthContext:
    return AuthContext(
        user_id=message.from_user.id if message.from_user else None,
        chat_type=message.chat.type if message.chat else "private",
    )


async def require_admin(message: Message, admin_ids: Iterable[int]) -> bool:
    """Enforce admin-only command. Returns True if the caller is allowed.

    Silently logs and does not respond when used in a group, to avoid teaching
    bystanders that admin commands exist on this bot.
    """
    ctx = auth_ctx(message)
    if not is_admin(ctx, admin_ids):
        if ctx.chat_type != "private":
            log.warning(
                "Refused admin command %r from user %s in chat %s (%s)",
                message.text, ctx.user_id, message.chat.id if message.chat else "?",
                ctx.chat_type,
            )
            return False
        await message.reply("This command is admin-only.")
        return False
    return True


async def get_or_create_chat_config(rt: Runtime, message: Message):
    """Ensure a chat is registered and return its config row."""
    assert message.chat is not None
    await rt.chats.upsert_chat(
        message.chat.id,
        message.chat.type,
        message.chat.title,
    )
    if message.from_user:
        await rt.users.upsert_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
            message.from_user.last_name,
            message.from_user.is_bot,
        )
    cfg = await rt.chats.get_config(message.chat.id)
    if cfg is None:
        s = rt.settings
        default_policy = (
            s.default_response_policy_private
            if message.chat.type == "private"
            else s.default_response_policy_group
        )
        cfg = await rt.chats.upsert_default_config(
            message.chat.id,
            response_policy=default_policy,
            ambient_probability=s.default_ambient_probability,
            persona=s.default_persona,
            duckhunt_enabled=s.duckhunt_enabled_by_default,
        )
    return cfg


def display_name(user) -> str:
    if not user:
        return "anonymous"
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    return name or (user.username or f"user{user.id}")
