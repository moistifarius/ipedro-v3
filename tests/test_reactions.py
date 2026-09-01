"""The bot must know it reacted, and own the reaction when asked.

Regression: the ambient emoji reaction chose its emoji inline inside the
Telegram call and persisted nothing, so the bot had no idea it had ever
reacted. Asked "what are those reactions about", it answered "which
reactions? I'm not seeing what you're pointing at."
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ipedro.handlers import chat
from ipedro.memory.context_builder import REACTION_NOTE_PREFIX, reaction_note
from tests.test_captcha_intercept import _msg, _rt_with


def _handler(rt):
    router = chat.build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == "on_message")


@pytest.fixture
def always_react(monkeypatch):
    """Force the reaction roll, suppress the Dale sprinkle, and stop
    on_message before the AI pipeline."""
    monkeypatch.setattr(chat, "_REACT_PROBABILITY", 1.0)
    monkeypatch.setattr(chat, "_DALE_GIF_PROBABILITY", 0.0)
    monkeypatch.setattr(chat, "should_respond", lambda *a, **k: False)
    monkeypatch.setattr(chat, "maybe_summarize", AsyncMock())


def _rt(*, memory=True):
    rt = _rt_with()
    rt.chats.get_config.return_value.memory_enabled = memory
    rt.chats.get_config.return_value.response_policy = "mention"
    rt.memory.record_message = AsyncMock()
    rt.memory.recent_messages = AsyncMock(return_value=[])
    return rt


def _recorded_reaction(rt):
    """The synthetic assistant turn written for the reaction, if any."""
    for call in rt.memory.record_message.await_args_list:
        kw = call.kwargs
        if kw.get("role") == "assistant" and REACTION_NOTE_PREFIX in kw["content"]:
            return kw
    return None


@pytest.mark.asyncio
async def test_the_recorded_emoji_is_the_one_actually_sent(always_react):
    """The sharp one. The obvious wrong fix calls random.choice twice and
    records a different emoji than it reacted with."""
    rt = _rt()
    await _handler(rt)(_msg(text="I give up"))

    sent = rt.bot.set_message_reaction.await_args.kwargs["reaction"][0].emoji
    note = _recorded_reaction(rt)
    assert note is not None, "the reaction was not recorded at all"
    assert sent in note["content"], (sent, note["content"])


@pytest.mark.asyncio
async def test_the_record_quotes_the_message_reacted_to(always_react):
    rt = _rt()
    await _handler(rt)(_msg(text="I give up"))
    assert '"I give up"' in _recorded_reaction(rt)["content"]


@pytest.mark.asyncio
async def test_record_is_synthetic_and_unembedded(always_react):
    """message_id=None is the schema's synthetic path; do_embed=False keeps
    reaction rows out of semantic retrieval."""
    rt = _rt()
    await _handler(rt)(_msg(text="anything"))
    note = _recorded_reaction(rt)
    assert note["message_id"] is None
    assert note["user_id"] is None
    assert note["do_embed"] is False


@pytest.mark.asyncio
async def test_nothing_recorded_when_memory_is_off(always_react):
    rt = _rt(memory=False)
    await _handler(rt)(_msg(text="anything"))
    rt.bot.set_message_reaction.assert_awaited()      # still reacts
    rt.memory.record_message.assert_not_awaited()     # just doesn't store


@pytest.mark.asyncio
async def test_a_record_failure_does_not_break_the_handler(always_react):
    rt = _rt()

    async def _fail_only_the_reaction(**kw):
        if REACTION_NOTE_PREFIX in kw.get("content", ""):
            raise RuntimeError("db down")
        return 1

    rt.memory.record_message = AsyncMock(side_effect=_fail_only_the_reaction)
    await _handler(rt)(_msg(text="anything"))          # must not raise
    rt.bot.set_message_reaction.assert_awaited()       # reaction still landed


@pytest.mark.asyncio
async def test_a_failed_reaction_is_not_recorded(always_react):
    """If Telegram refused the reaction, claiming it happened would be worse
    than saying nothing."""
    rt = _rt()
    rt.bot.set_message_reaction = AsyncMock(side_effect=RuntimeError("nope"))
    await _handler(rt)(_msg(text="anything"))
    assert _recorded_reaction(rt) is None


@pytest.mark.asyncio
async def test_no_reaction_no_record(monkeypatch):
    monkeypatch.setattr(chat, "_REACT_PROBABILITY", 0.0)
    monkeypatch.setattr(chat, "_DALE_GIF_PROBABILITY", 0.0)
    monkeypatch.setattr(chat, "should_respond", lambda *a, **k: False)
    monkeypatch.setattr(chat, "maybe_summarize", AsyncMock())
    rt = _rt()
    await _handler(rt)(_msg(text="anything"))
    rt.bot.set_message_reaction.assert_not_awaited()
    assert _recorded_reaction(rt) is None


# ── the persona has to own it ────────────────────────────────────────────────

def test_reaction_note_round_trips_through_the_detector():
    note = reaction_note("🤡", "I give up")
    assert note == '(reacted 🤡 to: "I give up")'
    assert REACTION_NOTE_PREFIX in note


def test_reaction_note_truncates_long_messages():
    note = reaction_note("🤔", "x" * 200)
    assert len(note) < 100 and note.endswith('…")')


def test_reaction_note_collapses_whitespace():
    assert reaction_note("👍", "two\n\nlines  here") == \
        '(reacted 👍 to: "two lines here")'
