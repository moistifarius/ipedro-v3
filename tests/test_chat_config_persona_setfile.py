"""Tests for /chat_config persona <name> setfile — a per-chat persona_custom

override delivered as a .txt file, for prompts too long to fit in one
Telegram message (4096 chars) as inline /chat_config text. Mirrors
/master_prompt setfile (admin.py) but scoped to the chat the command runs
in, via rt.chats.update_config(chat_id, persona_custom=...).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers.ai import build_router


def _find_handler(router, name):
    for h in router.observers["message"].handlers:
        if h.callback.__name__ == name:
            return h.callback
    raise AssertionError(f"handler {name} not registered")


def _rt(*, admin_ids=frozenset({1}), member_status="administrator"):
    cfg = SimpleNamespace(
        response_policy="mention", ambient_probability=0.03, persona="dude",
        duckhunt_enabled=False, share_photo_enabled=False, comic_enabled=False,
        fortune_enabled=False, voice_transcribe=True, memory_enabled=True,
        ether_enabled=False, duck_names_public=True,
        monthly_recap_enabled=True, automod_enabled=True,
    )
    chats = SimpleNamespace(
        upsert_chat=AsyncMock(),
        get_config=AsyncMock(return_value=cfg),
        update_config=AsyncMock(),
    )
    users = SimpleNamespace(upsert_user=AsyncMock())
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status=member_status),
        ),
        get_file=AsyncMock(return_value=SimpleNamespace(file_path="f.txt")),
        download_file=AsyncMock(),
    )
    settings = SimpleNamespace(admin_ids=admin_ids, context_max_tokens=6000)
    return SimpleNamespace(chats=chats, users=users, bot=bot, settings=settings)


def _msg(
    *, caption=None, text=None, document=None, reply_to=None,
    chat_id=-500, user_id=1,
):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="group", title="soup chat"),
        from_user=SimpleNamespace(
            id=user_id, username="u", first_name="U", last_name=None,
            is_bot=False,
        ),
        text=text, caption=caption, document=document,
        reply_to_message=reply_to,
        bot=None,  # patched per-test to the rt's bot below
        reply=AsyncMock(),
    )


def _wire_bot(msg, rt):
    msg.bot = rt.bot
    return msg


@pytest.mark.asyncio
async def test_setfile_via_caption_sets_persona_custom_for_this_chat():
    rt = _rt()
    handler = _find_handler(build_router(rt), "chat_config_cmd")
    doc = SimpleNamespace(file_id="abc", file_size=100)
    msg = _wire_bot(
        _msg(caption="/chat_config persona soup setfile", document=doc,
             chat_id=-5283277496),
        rt,
    )
    prompt_text = "you are dale. " * 400  # comfortably over 4096 chars
    rt.bot.download_file.side_effect = (
        lambda path, destination: destination.write(prompt_text.encode())
    )
    await handler(msg)
    rt.chats.update_config.assert_awaited_once_with(
        -5283277496, persona="soup", persona_custom=prompt_text.strip(),
    )
    msg.reply.assert_awaited()
    body = msg.reply.await_args.args[0]
    assert "updated for this chat" in body


@pytest.mark.asyncio
async def test_setfile_via_reply_to_document_also_works():
    rt = _rt()
    handler = _find_handler(build_router(rt), "chat_config_cmd")
    doc = SimpleNamespace(file_id="abc", file_size=100)
    reply_to = SimpleNamespace(document=doc)
    msg = _wire_bot(
        _msg(text="/chat_config persona soup setfile", reply_to=reply_to),
        rt,
    )
    rt.bot.download_file.side_effect = (
        lambda path, destination: destination.write(b"a custom prompt")
    )
    await handler(msg)
    rt.chats.update_config.assert_awaited_once_with(
        -500, persona="soup", persona_custom="a custom prompt",
    )


@pytest.mark.asyncio
async def test_setfile_without_any_document_prompts_for_one():
    rt = _rt()
    handler = _find_handler(build_router(rt), "chat_config_cmd")
    msg = _wire_bot(
        _msg(text="/chat_config persona soup setfile"), rt,
    )
    await handler(msg)
    rt.chats.update_config.assert_not_called()
    body = msg.reply.await_args.args[0]
    assert "setfile" in body


@pytest.mark.asyncio
async def test_setfile_rejects_oversized_file():
    rt = _rt()
    handler = _find_handler(build_router(rt), "chat_config_cmd")
    doc = SimpleNamespace(file_id="abc", file_size=999_999)
    msg = _wire_bot(
        _msg(caption="/chat_config persona soup setfile", document=doc),
        rt,
    )
    await handler(msg)
    rt.chats.update_config.assert_not_called()
    rt.bot.get_file.assert_not_called()
    body = msg.reply.await_args.args[0]
    assert "cap is" in body


@pytest.mark.asyncio
async def test_setfile_rejects_non_utf8_file():
    rt = _rt()
    handler = _find_handler(build_router(rt), "chat_config_cmd")
    doc = SimpleNamespace(file_id="abc", file_size=10)
    msg = _wire_bot(
        _msg(caption="/chat_config persona soup setfile", document=doc), rt,
    )
    rt.bot.download_file.side_effect = (
        lambda path, destination: destination.write(b"\xff\xfe\x00bad")
    )
    await handler(msg)
    rt.chats.update_config.assert_not_called()
    body = msg.reply.await_args.args[0]
    assert "UTF-8" in body


@pytest.mark.asyncio
async def test_setfile_rejects_empty_file_without_clobbering():
    rt = _rt()
    handler = _find_handler(build_router(rt), "chat_config_cmd")
    doc = SimpleNamespace(file_id="abc", file_size=0)
    msg = _wire_bot(
        _msg(caption="/chat_config persona soup setfile", document=doc), rt,
    )
    rt.bot.download_file.side_effect = (
        lambda path, destination: destination.write(b"   \n  ")
    )
    await handler(msg)
    rt.chats.update_config.assert_not_called()
    body = msg.reply.await_args.args[0]
    assert "empty" in body.lower()


@pytest.mark.asyncio
async def test_setfile_blocked_for_non_admin_non_chat_admin():
    rt = _rt(admin_ids=frozenset(), member_status="member")
    handler = _find_handler(build_router(rt), "chat_config_cmd")
    doc = SimpleNamespace(file_id="abc", file_size=10)
    msg = _wire_bot(
        _msg(caption="/chat_config persona soup setfile", document=doc), rt,
    )
    await handler(msg)
    rt.chats.update_config.assert_not_called()
    rt.bot.get_file.assert_not_called()
    body = msg.reply.await_args.args[0]
    assert "admin" in body.lower()


@pytest.mark.asyncio
async def test_setfile_warns_when_prompt_eats_most_of_context_budget():
    rt = _rt()
    rt.settings.context_max_tokens = 100
    handler = _find_handler(build_router(rt), "chat_config_cmd")
    doc = SimpleNamespace(file_id="abc", file_size=999)
    msg = _wire_bot(
        _msg(caption="/chat_config persona soup setfile", document=doc), rt,
    )
    long_text = "word " * 500
    rt.bot.download_file.side_effect = (
        lambda path, destination: destination.write(long_text.encode())
    )
    await handler(msg)
    body = msg.reply.await_args.args[0]
    assert "exceeds context_max_tokens" in body


@pytest.mark.asyncio
async def test_inline_persona_text_still_works_unaffected():
    """Regression: the short inline-text path (no file) must be untouched."""
    rt = _rt()
    handler = _find_handler(build_router(rt), "chat_config_cmd")
    msg = _wire_bot(
        _msg(text="/chat_config persona soup a short custom prompt"), rt,
    )
    await handler(msg)
    rt.chats.update_config.assert_awaited_once_with(
        -500, persona="soup", persona_custom="a short custom prompt",
    )
