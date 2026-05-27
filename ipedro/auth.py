"""Admin authorization. Admin commands MUST go through is_admin()."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class AuthContext:
    """Minimal information needed to authorize a request."""

    user_id: int | None
    chat_type: str  # "private" | "group" | "supergroup" | "channel"


def is_admin(ctx: AuthContext, admin_ids: Iterable[int]) -> bool:
    """Return True only if the user is an admin AND the request is in a private chat.

    Admin commands are gated to private DMs to avoid leaking sensitive output
    (logs, memory, chat lists, send_message-as-bot) into group chats.
    """
    if ctx.user_id is None:
        return False
    if ctx.chat_type != "private":
        return False
    return ctx.user_id in set(admin_ids)


def is_admin_user(user_id: int | None, admin_ids: Iterable[int]) -> bool:
    """User-only admin check, without the private-chat requirement.

    Use this only for non-sensitive features (e.g. allowing the admin to set a
    chat's persona from inside the group). Never use this for log/memory access.
    """
    if user_id is None:
        return False
    return user_id in set(admin_ids)
