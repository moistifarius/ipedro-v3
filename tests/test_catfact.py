"""The /catfact command, the cat-fact fallback, and the ambient intercept.

Regressions covered:
- when the cheap AI model returns nothing, the bot used to send a bare 🐈
  emoji. It must instead deliver a real (dubious) cat fact, catified.
- the ambient cat-mention intercept in chat.py fired even in commands-only
  chats (no policy gate).
- a memory failure AFTER the cat-fact reply was sent crashed the handler
  instead of being logged.
"""

from __future__ import annotations

import logging
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers.ai import build_router
from ipedro.handlers.chat import build_router as build_chat_router
from ipedro.handlers.common import catify, fallback_cat_fact


def test_fallback_cat_fact_always_catifies_to_pussy():
    for seed in range(30):
        fact = fallback_cat_fact(random.Random(seed))
        assert "pussy" in catify(fact) or "pussies" in catify(fact)


def _catfact_handler(rt):
    router = build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == "catfact")


def _rt(*, fact):
    return SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset()),
        openai=SimpleNamespace(cheap_completion=AsyncMock(return_value=fact)),
        db=SimpleNamespace(), chats=SimpleNamespace(), users=SimpleNamespace(),
        bot=SimpleNamespace(),
    )


def _msg():
    return SimpleNamespace(
        chat=SimpleNamespace(id=100, type="group"),
        from_user=SimpleNamespace(id=7, is_bot=False),
        bot=SimpleNamespace(send_chat_action=AsyncMock()),
        reply=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_catfact_falls_back_to_a_real_fact_when_ai_empty():
    handler = _catfact_handler(_rt(fact=None))
    msg = _msg()
    await handler(msg)
    sent = msg.reply.await_args.args[0]
    assert sent != "🐈"
    assert "pussy" in sent or "pussies" in sent   # a catified cat fact, not an emoji


@pytest.mark.asyncio
async def test_catfact_catifies_the_ai_result():
    handler = _catfact_handler(_rt(fact="cats secretly run the government"))
    msg = _msg()
    await handler(msg)
    assert msg.reply.await_args.args[0] == "pussies secretly run the government"


# ---------------------------------------------------------------------------
# Ambient cat-mention intercept in chat.on_message (stub pattern mirrors
# tests/test_captcha_intercept.py).
# ---------------------------------------------------------------------------


def _on_message_handler(rt):
    router = build_chat_router(rt)
    for h in router.observers["message"].handlers:
        if h.callback.__name__ == "on_message":
            return h.callback
    raise AssertionError("on_message not registered")


def _chat_rt(
    *, response_policy="always", memory_enabled=False,
    record_side_effect=None, fact="cats invented the internet",
):
    cfg = SimpleNamespace(
        duckhunt_enabled=False, memory_enabled=memory_enabled,
        voice_transcribe=False, response_policy=response_policy,
        ambient_probability=0.0, persona="dude", persona_custom=None,
        automod_enabled=False,
    )
    chats = SimpleNamespace(
        upsert_chat=AsyncMock(),
        get_config=AsyncMock(return_value=cfg),
        upsert_default_config=AsyncMock(return_value=cfg),
    )
    users = SimpleNamespace(upsert_user=AsyncMock())
    bot = SimpleNamespace(
        me=AsyncMock(return_value=SimpleNamespace(id=1, username="botname")),
        set_message_reaction=AsyncMock(),
        send_chat_action=AsyncMock(),
    )
    db = SimpleNamespace(
        fetchrow=AsyncMock(return_value=None),
        fetchval=AsyncMock(return_value=0),
        execute=AsyncMock(return_value="OK"),
        fetch=AsyncMock(return_value=[]),
    )
    memory = SimpleNamespace(
        record_message=AsyncMock(side_effect=record_side_effect),
    )
    return SimpleNamespace(
        settings=SimpleNamespace(
            admin_ids=frozenset(), context_max_tokens=6000,
        ),
        bot=bot, db=db, chats=chats, users=users,
        duckhunt=SimpleNamespace(),
        openai=SimpleNamespace(cheap_completion=AsyncMock(return_value=fact)),
        memory=memory,
        persona_state=SimpleNamespace(),
    )


def _chat_msg(text="i love cats", chat_id=100, user_id=7):
    chat = SimpleNamespace(id=chat_id, type="group", title="t")
    from_user = SimpleNamespace(
        id=user_id, is_bot=False, username="u",
        first_name="U", last_name=None,
    )
    return SimpleNamespace(
        chat=chat, from_user=from_user, text=text, caption=None,
        voice=None, message_id=200, entities=None,
        reply_to_message=None,
        reply=AsyncMock(return_value=SimpleNamespace(message_id=201)),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=202)),
    )


@pytest.mark.asyncio
async def test_no_cat_fact_in_commands_only_chat():
    """The cat intercept must respect the commands-only opt-out: a chat
    that asked for zero ambient chatter gets zero ambient cat facts."""
    rt = _chat_rt(response_policy="commands")
    handler = _on_message_handler(rt)
    msg = _chat_msg(text="i love cats")
    await handler(msg)
    rt.openai.cheap_completion.assert_not_called()
    msg.reply.assert_not_called()
    msg.answer.assert_not_called()


@pytest.mark.asyncio
async def test_cat_fact_still_fires_under_normal_policy():
    """Sanity companion to the gate test: 'always' policy still facts."""
    rt = _chat_rt(response_policy="always")
    handler = _on_message_handler(rt)
    msg = _chat_msg(text="i love cats")
    await handler(msg)
    msg.reply.assert_awaited_once()
    assert "pussies" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_record_failure_after_cat_fact_send_does_not_raise(caplog):
    """The user already saw the reply; a DB hiccup while recording the
    bot's own turn must be logged, never raised out of the handler."""
    async def _record(**kwargs):
        if kwargs.get("role") == "assistant":
            raise RuntimeError("db down")

    rt = _chat_rt(memory_enabled=True, record_side_effect=_record)
    handler = _on_message_handler(rt)
    msg = _chat_msg(text="i love cats")
    with caplog.at_level(logging.WARNING, logger="ipedro.handlers.chat"):
        await handler(msg)  # must complete without raising
    # The reply went out before the failure…
    msg.reply.assert_awaited_once()
    assert "pussies" in msg.reply.await_args.args[0]
    # …both records were attempted (inbound user turn + bot turn)…
    assert rt.memory.record_message.await_count == 2
    # …and the failure was logged, not swallowed silently.
    assert "post-send memory record failed" in caplog.text
