"""Admin commands tell the admin when they did NOT work.

Both of these used to leave the admin with silence: /send_message to a chat
the bot was removed from raised Forbidden, which only the log heard, and
/config_for with a mistyped chat id hit a foreign-key error the same way.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter,
)

from ipedro.handlers.admin import build_router


def _handler(rt, name):
    router = build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == name)


def _msg(text):
    return SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private", title=None),
        from_user=SimpleNamespace(id=7, is_bot=False, username="admin"),
        text=text, reply=AsyncMock(),
    )


def _rt(**over):
    chats = SimpleNamespace(
        get_config=AsyncMock(return_value=None),
        upsert_default_config=AsyncMock(),
        list_known=AsyncMock(return_value=[]),
    )
    rt = SimpleNamespace(
        settings=SimpleNamespace(
            admin_ids=frozenset({7}), default_response_policy_group="mention",
            default_ambient_probability=0.03, default_persona="dude",
            duckhunt_enabled_by_default=False, share_photo_enabled_by_default=False,
        ),
        bot=SimpleNamespace(send_message=AsyncMock()), chats=chats, db=SimpleNamespace(),
    )
    for k, v in over.items():
        setattr(rt, k, v)
    return rt


@pytest.mark.asyncio
async def test_a_delivered_message_says_sent():
    rt = _rt()
    msg = _msg("/send_message -100123 hello there")
    await _handler(rt, "send_message_cmd")(msg)
    rt.bot.send_message.assert_awaited_once_with(-100123, "hello there", disable_notification=True)
    assert msg.reply.await_args.args[0] == "Sent."


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    TelegramBadRequest(method=None, message="chat not found"),
    TelegramForbiddenError(method=None, message="bot was kicked from the group chat"),
    TelegramRetryAfter(method=None, message="Flood control", retry_after=12),
])
async def test_every_way_telegram_can_refuse_is_reported(error):
    rt = _rt()
    rt.bot.send_message.side_effect = error
    msg = _msg("/send_message -100123 hello there")
    await _handler(rt, "send_message_cmd")(msg)
    sent = msg.reply.await_args.args[0]
    assert sent.startswith("Send failed:") and sent != "Sent."


@pytest.mark.asyncio
async def test_config_for_an_unknown_chat_says_so_instead_of_going_quiet():
    rt = _rt()
    rt.chats.upsert_default_config.side_effect = asyncpg.ForeignKeyViolationError("no such chat")
    msg = _msg("/config_for -1001234567890")
    await _handler(rt, "config_for")(msg)
    assert "never seen that chat" in msg.reply.await_args.args[0]
    assert "/list_chat_ids" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_a_memory_wipe_leaves_an_audit_row():
    """There is no undo, so /cmdlog must show who wiped what."""
    rt = _rt()
    rt.memory = SimpleNamespace(wipe_conversation=AsyncMock(
        return_value={"messages": 12, "summaries": 1}))
    rt.command_log = SimpleNamespace(add=AsyncMock())
    msg = _msg("/memory_wipe -1009876543210 facts")
    await _handler(rt, "memory_wipe")(msg)
    rt.memory.wipe_conversation.assert_awaited_once_with(-1009876543210, include_facts=True)
    chat, user, command, args, ok = rt.command_log.add.await_args.args[:5]
    assert (chat, user, command, ok) == (42, 7, "/memory_wipe", True)
    assert "chat=-1009876543210" in args and "facts=True" in args and "12 messages" in args


# ── small admin-command corrections ──────────────────────────────────────────

def test_a_toast_is_always_short_enough_for_telegram():
    from ipedro.handlers.admin import _toast

    long_error = "duplicate key value violates unique constraint " + "x" * 400
    assert len(_toast("DB error: ", long_error)) <= 200
    assert _toast("Failed: ", "nope") == "Failed: nope"


@pytest.mark.parametrize("token,is_chat", [
    ("-1001234567890", True), ("-5", True), ("123456789", True),
    ("2019", False), ("90", False), ("3", False), ("road", False), ("-", False),
])
def test_only_something_shaped_like_a_chat_id_is_taken_for_one(token, is_chat):
    from ipedro.handlers.admin import _looks_like_chat_id

    assert _looks_like_chat_id(token) is is_chat


@pytest.mark.asyncio
async def test_a_search_that_starts_with_a_number_is_a_search_not_a_chat():
    """"/memory_search 2019 road trip" was read as chat 2019, query "road trip"."""
    rt = _rt()
    msg = _msg("/memory_search 2019 road trip")
    await _handler(rt, "memory_search")(msg)
    assert "No known chats yet." in msg.reply.await_args.args[0]    # went to the picker


@pytest.mark.asyncio
@pytest.mark.parametrize("command,handler", [
    ("/duckstats_reset -100 John Smith", "duckstats_reset"),
    ("/duckstats_edit -100 John Smith", "duckstats_edit"),
])
async def test_a_display_name_of_several_words_is_looked_up_whole(monkeypatch, command, handler):
    from ipedro.handlers import admin

    resolve = AsyncMock(return_value=None)
    monkeypatch.setattr(admin, "_resolve_user_id", resolve)
    msg = _msg(command)
    await _handler(_rt(), handler)(msg)
    assert resolve.await_args.args[2] == "John Smith"
    assert "'John Smith' not found" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_the_delete_last_toast_says_what_really_happened(monkeypatch):
    """It said "Deleted." even when nothing was tracked, or the delete failed."""
    from ipedro.handlers import admin

    rt = _rt()
    router = admin.build_router(rt)
    on_last = next(h.callback for h in router.observers["callback_query"].handlers
                   if h.callback.__name__ == "on_delete_last_confirm")

    monkeypatch.setattr(admin, "recent_tracked", lambda chat_id: [])
    cb = SimpleNamespace(
        data="dlast:-100:1:go", message=SimpleNamespace(edit_text=AsyncMock()),
        from_user=SimpleNamespace(id=7), answer=AsyncMock(),
    )
    await on_last(cb)
    assert cb.answer.await_args.args[0] == "Nothing to delete."
