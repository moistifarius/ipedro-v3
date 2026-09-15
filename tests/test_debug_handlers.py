"""Tests for the admin /debug_* handlers in ipedro/handlers/debug.py.

Regression coverage for the /debug_captcha (and friends) TypeError:
``_force_challenge`` used to pass a bogus ``intro=`` kwarg to
``_issue_bef_challenge`` (which has no such parameter), so every
/debug_challenge|captcha|trivia|recipe invocation blew up before any
reply was sent. These tests drive the real ``_issue_bef_challenge``
(real captcha generation) against a stubbed runtime/message.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers.debug import build_router


def _find_message_handler(router, name: str):
    for h in router.observers["message"].handlers:
        if h.callback.__name__ == name:
            return h.callback
    raise AssertionError(f"handler {name} not registered")


def _msg(*, chat_id=42, user_id=7, chat_type="private") -> SimpleNamespace:
    # require_admin only passes for admin ids in a private chat.
    chat = SimpleNamespace(id=chat_id, type=chat_type, title=None)
    from_user = SimpleNamespace(
        id=user_id, is_bot=False, username="admin",
        first_name="Admin", last_name=None,
    )
    return SimpleNamespace(
        chat=chat, from_user=from_user, text="/debug_captcha",
        reply=AsyncMock(),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=201)),
        answer_photo=AsyncMock(return_value=SimpleNamespace(message_id=200)),
    )


def _rt(*, admin_ids=frozenset({7})) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(admin_ids=admin_ids),
        duckhunt=SimpleNamespace(set_bef_challenge=AsyncMock()),
        openai=SimpleNamespace(cheap_completion=AsyncMock(return_value="")),
    )


@pytest.mark.asyncio
async def test_debug_captcha_issues_challenge_for_admin():
    """/debug_captcha must not raise and must store a pending captcha.

    The captcha path renders a real image, sends it via answer_photo,
    and persists the answer with the prompt's message_id.
    """
    rt = _rt()
    handler = _find_message_handler(build_router(rt), "debug_captcha")
    msg = _msg()
    await handler(msg)  # would raise TypeError before the fix
    msg.answer_photo.assert_awaited_once()
    rt.duckhunt.set_bef_challenge.assert_awaited_once()
    args = rt.duckhunt.set_bef_challenge.await_args.args
    assert args[0] == 42          # chat_id
    assert args[1] == 7           # user_id
    assert args[3] == "captcha"   # kind
    assert args[4] == 200         # prompt message_id
    # No "Failed to issue challenge" fallback reply.
    msg.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_debug_captcha_ignored_for_non_admin_in_group():
    """Non-admin in a group: silently refused, nothing issued."""
    rt = _rt(admin_ids=frozenset({999}))
    handler = _find_message_handler(build_router(rt), "debug_captcha")
    msg = _msg(user_id=7, chat_type="group")
    await handler(msg)
    msg.answer_photo.assert_not_awaited()
    rt.duckhunt.set_bef_challenge.assert_not_awaited()
    msg.reply.assert_not_awaited()
