"""Tests for the free-text captcha answer intercept in chat router.

The intercept lives in ``ipedro/handlers/chat.py`` inside the catch-all
``on_message`` handler. Two paths surface a pending challenge:

  1. ``msg.reply_to_message`` matches the prompt message (formal reply).
  2. The user has an outstanding ``bef_challenges`` row in this chat.

These tests stub out the runtime + duckhunt service and exercise the
handler directly. The handler chains to memory + AI on the no-challenge
path; we don't actually run that branch in these tests — the goal is
just to confirm the intercept fires (or doesn't) in each scenario.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.duckhunt.service import PendingBefChallenge
from ipedro.handlers.chat import build_router


def _find_message_handler(router, name: str):
    for h in router.observers["message"].handlers:
        if h.callback.__name__ == name:
            return h.callback
    raise AssertionError(f"handler {name} not registered")


def _challenge(
    *, chat_id=42, user_id=7, answer="RMY4X", prompt_message_id=99,
    created_at=None,
):
    """Build a PendingBefChallenge for a captcha — stored answer IS the text."""
    return PendingBefChallenge(
        chat_id=chat_id, user_id=user_id, challenge=answer, kind="captcha",
        prompt_message_id=prompt_message_id,
        created_at=created_at or datetime.now(timezone.utc),
    )


def _msg(
    *, text="RMY4X", chat_id=42, user_id=7, reply_to=None,
) -> SimpleNamespace:
    chat = SimpleNamespace(id=chat_id, type="group", title="t")
    from_user = SimpleNamespace(
        id=user_id, is_bot=False, username="u",
        first_name="U", last_name=None,
    )
    return SimpleNamespace(
        chat=chat, from_user=from_user, text=text, caption=None,
        voice=None, message_id=100, entities=None,
        photo=None, sticker=None, animation=None, video=None,
        video_note=None, document=None, audio=None,
        reply_to_message=reply_to,
        reply=AsyncMock(),
        answer=AsyncMock(),
    )


def _rt_with(
    *, find_by_prompt=None, get_by_user=None, duckhunt_enabled=True,
):
    """Build a minimal Runtime stub with a fake duckhunt service.

    ``find_by_prompt`` and ``get_by_user`` are the canned return values
    for the two lookups inside the intercept. None means "no match".
    """
    cfg = SimpleNamespace(
        duckhunt_enabled=duckhunt_enabled, memory_enabled=False,
        voice_transcribe=False, response_policy="always",
        ambient_probability=0.0, persona="dude", persona_custom=None,
        automod_enabled=True,
        vision_enabled=False,
    )
    chats = SimpleNamespace(
        upsert_chat=AsyncMock(),
        get_config=AsyncMock(return_value=cfg),
        upsert_default_config=AsyncMock(return_value=cfg),
    )
    users = SimpleNamespace(upsert_user=AsyncMock())
    duckhunt = SimpleNamespace(
        find_bef_challenge_by_prompt=AsyncMock(return_value=find_by_prompt),
        get_bef_challenge=AsyncMock(return_value=get_by_user),
        clear_bef_challenge=AsyncMock(),
        active_duck=AsyncMock(return_value=None),
    )
    bot = SimpleNamespace(
        me=AsyncMock(return_value=SimpleNamespace(id=1, username="botname")),
        set_message_reaction=AsyncMock(),
        send_chat_action=AsyncMock(),
    )
    settings = SimpleNamespace(admin_ids=frozenset(), context_max_tokens=6000)
    db = SimpleNamespace(
        fetchrow=AsyncMock(return_value=None),
        fetchval=AsyncMock(return_value=0),
        execute=AsyncMock(return_value="OK"),
        fetch=AsyncMock(return_value=[]),
    )
    return SimpleNamespace(
        settings=settings, bot=bot, db=db,
        chats=chats, users=users, duckhunt=duckhunt,
        openai=SimpleNamespace(chat=AsyncMock(return_value="PASS: ok")),
        memory=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_intercept_matches_formal_reply():
    """Path 1: user uses Telegram's reply feature on the prompt photo.

    The intercept must call find_bef_challenge_by_prompt with the
    prompt's message_id and short-circuit before any other handling.
    """
    challenge = _challenge(prompt_message_id=99)
    rt = _rt_with(find_by_prompt=challenge)
    on_message = _find_message_handler(build_router(rt), "on_message")
    reply = SimpleNamespace(message_id=99)
    msg = _msg(text="RMY4X", reply_to=reply)
    await on_message(msg)
    # Lookup-by-prompt was used; the fallback lookup was NOT.
    rt.duckhunt.find_bef_challenge_by_prompt.assert_awaited_once()
    rt.duckhunt.get_bef_challenge.assert_not_called()
    # The challenge cleared (captcha matched) and the user was replied to.
    rt.duckhunt.clear_bef_challenge.assert_awaited_once()
    msg.reply.assert_awaited()


@pytest.mark.asyncio
async def test_intercept_matches_free_text_when_pending():
    """Path 2: user types the answer with no formal reply.

    With no reply_to_message, the intercept should fall back to
    get_bef_challenge and still resolve the captcha when the answer
    matches. This is the bug Fix 1 addressed.
    """
    challenge = _challenge()
    rt = _rt_with(find_by_prompt=None, get_by_user=challenge)
    on_message = _find_message_handler(build_router(rt), "on_message")
    msg = _msg(text="RMY4X", reply_to=None)
    await on_message(msg)
    # Free-text path: no prompt lookup, fallback to user lookup.
    rt.duckhunt.find_bef_challenge_by_prompt.assert_not_called()
    rt.duckhunt.get_bef_challenge.assert_awaited_once()
    # Captcha matched the free-text answer → challenge cleared.
    rt.duckhunt.clear_bef_challenge.assert_awaited_once()
    msg.reply.assert_awaited()


@pytest.mark.asyncio
async def test_no_intercept_when_no_pending_challenge():
    """Without a pending challenge the message must flow past the
    intercept into the regular chat handler.

    We can't easily exercise the rest of on_message without a much
    richer stub, but we can confirm: clear_bef_challenge was NOT called
    (so the intercept did NOT fire) and the lookup paths returned None
    as expected.
    """
    rt = _rt_with(find_by_prompt=None, get_by_user=None)
    on_message = _find_message_handler(build_router(rt), "on_message")
    msg = _msg(text="hello there", reply_to=None)
    # The handler will progress past the intercept and into the regular
    # flow. The regular flow accesses many things we haven't stubbed;
    # we catch + ignore any AttributeError. The intercept assertions
    # below are what we care about.
    try:
        await on_message(msg)
    except Exception:
        pass
    rt.duckhunt.get_bef_challenge.assert_awaited_once()
    # The user lookup returned None, so the intercept did NOT clear or
    # judge a challenge.
    rt.duckhunt.clear_bef_challenge.assert_not_called()


@pytest.mark.asyncio
async def test_intercept_skipped_when_duckhunt_disabled():
    rt = _rt_with(duckhunt_enabled=False, get_by_user=_challenge())
    on_message = _find_message_handler(build_router(rt), "on_message")
    msg = _msg(text="RMY4X", reply_to=None)
    try:
        await on_message(msg)
    except Exception:
        pass
    # With duckhunt off, neither lookup should run.
    rt.duckhunt.find_bef_challenge_by_prompt.assert_not_called()
    rt.duckhunt.get_bef_challenge.assert_not_called()


@pytest.mark.asyncio
async def test_stale_challenge_is_cleared_and_not_judged():
    """A challenge older than the TTL must be silently cleared and the
    message allowed to flow through — never judged as a (failing) answer.

    Without this guard a forgotten challenge hijacks the chat forever,
    replying 'Not quite. Try again.' to everything (worst in a DM)."""
    stale = _challenge(
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    rt = _rt_with(get_by_user=stale)
    on_message = _find_message_handler(build_router(rt), "on_message")
    # Text that is NOT the captcha answer — if it were judged it'd fail.
    msg = _msg(text="hey what's up", reply_to=None)
    try:
        await on_message(msg)
    except Exception:
        pass
    # Stale challenge was cleared exactly once for the right (chat, user)…
    rt.duckhunt.clear_bef_challenge.assert_awaited_once_with(42, 7)
    # …and the bot did NOT reply with a judge verdict (no "Not quite").
    for call in msg.reply.await_args_list:
        assert "Not quite" not in (call.args[0] if call.args else "")


@pytest.mark.asyncio
async def test_command_mid_challenge_is_not_judged():
    """A slash-command typed while a challenge is pending must not be
    treated as an answer — the escape hatches have to stay reachable."""
    challenge = _challenge()
    rt = _rt_with(get_by_user=challenge)
    on_message = _find_message_handler(build_router(rt), "on_message")
    msg = _msg(text="/chat_config duckhunt off", reply_to=None)
    try:
        await on_message(msg)
    except Exception:
        pass
    # The intercept never ran its lookups for a command message.
    rt.duckhunt.get_bef_challenge.assert_not_called()
    rt.duckhunt.find_bef_challenge_by_prompt.assert_not_called()
    rt.duckhunt.clear_bef_challenge.assert_not_called()


@pytest.mark.asyncio
async def test_free_text_intercept_ignores_other_users_challenge():
    """Defense-in-depth: if get_bef_challenge somehow returns a row
    whose user_id doesn't match the sender, the intercept must NOT
    judge it as that sender's attempt. The schema PK prevents this
    in practice but the handler's guard is still load-bearing."""
    challenge = _challenge(user_id=999)  # different user
    rt = _rt_with(get_by_user=challenge)
    on_message = _find_message_handler(build_router(rt), "on_message")
    msg = _msg(text="RMY4X", user_id=7, reply_to=None)
    try:
        await on_message(msg)
    except Exception:
        pass
    # No clear_bef_challenge — the user_id mismatch guarded it.
    rt.duckhunt.clear_bef_challenge.assert_not_called()
