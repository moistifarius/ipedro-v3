"""The ambient Dale sprinkle: an occasional unprompted GIF in on_message."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ipedro import dale_gifs as dale
from ipedro.handlers import chat
from tests.test_captcha_intercept import _msg, _rt_with


def _handler(rt):
    router = chat.build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == "on_message")


@pytest.fixture
def sprinkle(monkeypatch):
    """Force the roll, stub the sender, and stop on_message before the AI."""
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(dale, "send_random", sent)
    monkeypatch.setattr(chat, "_DALE_GIF_PROBABILITY", 1.0)
    monkeypatch.setattr(chat, "_REACT_PROBABILITY", 0.0)
    monkeypatch.setattr(chat, "should_respond", lambda *a, **k: False)
    return sent


def _rt(*, policy="mention", automod=True):
    rt = _rt_with()
    cfg = rt.chats.get_config.return_value
    cfg.response_policy = policy
    cfg.automod_enabled = automod
    return rt


@pytest.mark.asyncio
async def test_fires_on_an_ordinary_unaddressed_message(sprinkle):
    rt = _rt()
    await _handler(rt)(_msg(text="anyway the kitchen tap is dripping"))
    sprinkle.assert_awaited_once()
    assert sprinkle.await_args.args[2] == ""      # any tag → any Dale GIF


@pytest.mark.asyncio
async def test_never_fires_when_the_bot_is_addressed_by_name(sprinkle):
    """A random GIF must never replace a real answer."""
    rt = _rt()
    await _handler(rt)(_msg(text="dale what do you reckon about this"))
    sprinkle.assert_not_awaited()


@pytest.mark.asyncio
async def test_never_fires_on_a_reply_to_the_bot(sprinkle, monkeypatch):
    rt = _rt()
    monkeypatch.setattr(chat, "_is_reply_to_bot", lambda *a, **k: True)
    await _handler(rt)(_msg(text="ordinary chatter"))
    sprinkle.assert_not_awaited()


@pytest.mark.asyncio
async def test_never_fires_in_a_commands_only_chat(sprinkle):
    rt = _rt(policy="commands")
    await _handler(rt)(_msg(text="ordinary chatter"))
    sprinkle.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_automod_switch_turns_it_off(sprinkle):
    rt = _rt(automod=False)
    await _handler(rt)(_msg(text="ordinary chatter"))
    sprinkle.assert_not_awaited()


@pytest.mark.asyncio
async def test_probability_actually_gates_it(monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(dale, "send_random", sent)
    monkeypatch.setattr(chat, "_DALE_GIF_PROBABILITY", 0.0)
    monkeypatch.setattr(chat, "_REACT_PROBABILITY", 0.0)
    monkeypatch.setattr(chat, "should_respond", lambda *a, **k: False)
    await _handler(_rt())(_msg(text="ordinary chatter"))
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_send_failure_never_breaks_the_message_pipeline(
    monkeypatch, caplog,
):
    monkeypatch.setattr(dale, "send_random",
                        AsyncMock(side_effect=RuntimeError("telegram said no")))
    monkeypatch.setattr(chat, "_DALE_GIF_PROBABILITY", 1.0)
    monkeypatch.setattr(chat, "_REACT_PROBABILITY", 0.0)
    monkeypatch.setattr(chat, "should_respond", lambda *a, **k: False)
    await _handler(_rt())(_msg(text="ordinary chatter"))   # must not raise


@pytest.mark.asyncio
async def test_an_empty_library_is_silent_not_noisy(monkeypatch):
    """No text fallback for the ambient path: with nothing stored the bot
    simply says nothing, rather than emitting a stray line 3% of the time."""
    monkeypatch.setattr(chat, "_DALE_GIF_PROBABILITY", 1.0)
    monkeypatch.setattr(chat, "_REACT_PROBABILITY", 0.0)
    monkeypatch.setattr(chat, "should_respond", lambda *a, **k: False)
    rt = _rt()
    rt.db.fetchrow = AsyncMock(return_value=None)     # empty library
    msg = _msg(text="ordinary chatter")
    await _handler(rt)(msg)
    msg.reply.assert_not_awaited()
    msg.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_it_does_not_return_early(sprinkle, monkeypatch):
    """Returning after the GIF would skip maybe_summarize below it."""
    summarize = AsyncMock()
    monkeypatch.setattr(chat, "maybe_summarize", summarize)
    rt = _rt()
    rt.chats.get_config.return_value.memory_enabled = True
    rt.memory.record_message = AsyncMock()
    await _handler(rt)(_msg(text="ordinary chatter"))
    sprinkle.assert_awaited_once()
    summarize.assert_awaited()          # flow continued past the sprinkle
