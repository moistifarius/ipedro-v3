"""The /catfact command and the cat-fact fallback.

Regression: when the cheap AI model returns nothing, the bot used to send a
bare 🐈 emoji. It must instead deliver a real (dubious) cat fact, catified.
"""

from __future__ import annotations

import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers.ai import build_router
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
