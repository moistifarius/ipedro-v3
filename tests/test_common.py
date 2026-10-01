"""Tests for ipedro.handlers.common's require_owner.

require_owner is the gate for requests more consequential than ordinary
admin commands (self-modification, bot-creation) — these tests pin that
it's genuinely stricter than require_admin, not just a rename.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers.common import require_owner

OWNER = 315660812


def _msg(*, user_id: int | None, chat_type: str = "private") -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id) if user_id is not None else None,
        chat=SimpleNamespace(id=1, type=chat_type),
        text="/evolve do a thing",
        reply=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_owner_in_dm_is_allowed():
    msg = _msg(user_id=OWNER)
    assert await require_owner(msg, OWNER) is True
    msg.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_admin_who_is_not_the_owner_is_refused():
    """The whole point of this gate: admin_ids membership doesn't count."""
    msg = _msg(user_id=999)
    assert await require_owner(msg, OWNER) is False
    msg.reply.assert_awaited_once()
    assert "owner" in msg.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_owner_in_a_group_is_refused_silently():
    """Same leak-prevention rule as require_admin: no reply in a group, so
    bystanders don't learn the command exists."""
    msg = _msg(user_id=OWNER, chat_type="supergroup")
    assert await require_owner(msg, OWNER) is False
    msg.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_anonymous_sender_is_refused():
    msg = _msg(user_id=None)
    assert await require_owner(msg, OWNER) is False
