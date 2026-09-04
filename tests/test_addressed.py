"""Talking to the bot without saying its name.

Two layers: a conversation window plus crisp follow-up openers (free), and
a cheap classifier for the ambiguous middle. The tests pin the cost shape
as much as the behaviour — the classifier must not fire when the bot has
been quiet, or it becomes a per-message tax on every chat.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import addressed
from ipedro.handlers import chat
from ipedro.memory.context_builder import BuiltContext
from tests.test_captcha_intercept import _msg, _rt_with

CHAT = 42


# ── the window ───────────────────────────────────────────────────────────────

def test_window_opens_on_a_reply_and_closes_by_time():
    assert not addressed.in_conversation(CHAT)
    addressed.note_bot_reply(CHAT, now=1000.0)
    assert addressed.in_conversation(CHAT, now=1000.0 + 599)
    assert not addressed.in_conversation(CHAT, now=1000.0 + 601)


def test_window_closes_after_enough_other_messages():
    addressed.note_bot_reply(CHAT, now=1000.0)
    for _ in range(addressed._WINDOW_MESSAGES):
        addressed.note_user_message(CHAT)
    assert addressed.in_conversation(CHAT, now=1001.0)
    addressed.note_user_message(CHAT)            # one too many
    assert not addressed.in_conversation(CHAT, now=1001.0)


def test_a_new_reply_reopens_a_closed_window():
    addressed.note_bot_reply(CHAT, now=1000.0)
    for _ in range(20):
        addressed.note_user_message(CHAT)
    addressed.note_bot_reply(CHAT, now=2000.0)
    assert addressed.in_conversation(CHAT, now=2001.0)


# ── the free layer ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "why?", "why", "what do you mean", "prove it", "source?", "?", "??",
    "no way", "you sure", "lol what", "How come?",
])
def test_follow_up_openers_are_a_reply_inside_the_window(text):
    assert addressed.quick_verdict(text, in_conversation=True) is True


@pytest.mark.parametrize("text", [
    "why?", "prove it", "what do you mean",
])
def test_the_same_openers_mean_nothing_when_the_bot_has_been_quiet(text):
    """Outside the window 'why?' is aimed at whoever spoke last — not him."""
    assert addressed.quick_verdict(text, in_conversation=False) is False


@pytest.mark.parametrize("text", [
    "why would you say that",          # second person: could be him
    "did you see the game?",           # a question: could be him
    "did you guys see the game?",      # plural you: probably the room
])
def test_ambiguous_lines_inside_the_window_go_to_the_classifier(text):
    assert addressed.quick_verdict(text, in_conversation=True) is None


def test_plain_chatter_inside_the_window_is_not_for_him():
    assert addressed.quick_verdict("the game was great", in_conversation=True) is False


def test_at_someone_else_is_never_for_him():
    assert addressed.quick_verdict("@matt why", in_conversation=True) is False


@pytest.mark.parametrize("text", [
    "anyone know when the game is", "does anybody remember his name",
    "can someone explain this", "thoughts?",
])
def test_a_question_to_the_room_earns_a_look_even_when_quiet(text):
    assert addressed.quick_verdict(text, in_conversation=False) is None


def test_ordinary_chatter_when_quiet_costs_nothing():
    assert addressed.quick_verdict("morning all", in_conversation=False) is False
    assert addressed.quick_verdict("", in_conversation=True) is False


# ── the model layer ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("answer,expected", [
    ("YES", True), ("yes.", True), ("Yes, it is", True),
    ("NO", False), ("no", False), ("", False), (None, False), ("maybe", False),
])
def test_classifier_answers_are_parsed_strictly(answer, expected):
    assert addressed._parse_yes(answer) is expected


@pytest.mark.asyncio
async def test_classifier_sees_the_recent_lines_with_the_bot_marked():
    openai = SimpleNamespace(cheap_completion=AsyncMock(return_value="YES"))
    hit = await addressed.classify(
        openai, speaker="Matt", text="why would you say that",
        recent=[("Luke", "propane is fine"), ("Dale", "propane is a government plot")],
        chat_id=CHAT,
    )
    assert hit is True
    prompt = openai.cheap_completion.await_args.args[0]
    assert "[Dale]: propane is a government plot" in prompt
    assert "NEW message from Matt: why would you say that" in prompt
    assert openai.cheap_completion.await_args.kwargs["max_tokens"] == 3


@pytest.mark.asyncio
async def test_wants_reply_skips_the_model_when_the_text_settles_it():
    rt = SimpleNamespace(openai=SimpleNamespace(cheap_completion=AsyncMock()),
                         memory=SimpleNamespace())
    addressed.note_bot_reply(CHAT)
    assert await addressed.wants_reply(rt, CHAT, speaker="Matt", text="why?",
                                       memory_enabled=True) is True
    assert await addressed.wants_reply(rt, CHAT, speaker="Matt", text="nice day",
                                       memory_enabled=True) is False
    rt.openai.cheap_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_wants_reply_pulls_recent_history_for_the_classifier():
    rows = [
        SimpleNamespace(role="user", author_name="Luke", content="propane is fine"),
        SimpleNamespace(role="assistant", author_name=None, content="it's a plot"),
    ]
    rt = SimpleNamespace(
        openai=SimpleNamespace(cheap_completion=AsyncMock(return_value="NO")),
        memory=SimpleNamespace(recent_messages=AsyncMock(return_value=rows)),
    )
    addressed.note_bot_reply(CHAT)
    hit = await addressed.wants_reply(rt, CHAT, speaker="Matt",
                                      text="did you see the game?", memory_enabled=True)
    assert hit is False
    prompt = rt.openai.cheap_completion.await_args.args[0]
    assert "[Dale]: it's a plot" in prompt and "[Luke]: propane is fine" in prompt


# ── end to end through on_message ────────────────────────────────────────────

def _handler(rt):
    router = chat.build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == "on_message")


def _mention_rt(monkeypatch, *, classifier="NO"):
    rt = _rt_with()
    cfg = rt.chats.get_config.return_value
    cfg.response_policy = "mention"
    for field in ("monthly_recap_enabled", "share_photo_enabled",
                  "comic_enabled", "fortune_enabled", "ether_enabled"):
        setattr(cfg, field, False)
    rt.openai = SimpleNamespace(
        chat=AsyncMock(return_value="sh-sha, it's a plot"),
        cheap_completion=AsyncMock(return_value=classifier),
    )

    async def fake_build(**kwargs):
        return BuiltContext(messages=[{"role": "user", "content": "x"}], tokens=1)

    monkeypatch.setattr(chat, "build_context", fake_build)
    monkeypatch.setattr(chat, "resolve_impersonation", AsyncMock(return_value=None))
    return rt


@pytest.mark.asyncio
async def test_a_follow_up_right_after_the_bot_spoke_gets_answered(monkeypatch):
    """No name, no reply-to. He said something thirty seconds ago and
    someone said 'why?' — that is for him."""
    rt = _mention_rt(monkeypatch)
    addressed.note_bot_reply(CHAT)
    msg = _msg(text="why?")
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    rt.openai.chat.assert_awaited_once()
    rt.openai.cheap_completion.assert_not_awaited()      # the free layer decided


@pytest.mark.asyncio
async def test_the_same_line_when_he_has_been_quiet_is_ignored(monkeypatch):
    rt = _mention_rt(monkeypatch)
    msg = _msg(text="why?")
    msg.answer = AsyncMock()
    await _handler(rt)(msg)
    rt.openai.chat.assert_not_awaited()
    rt.openai.cheap_completion.assert_not_awaited()      # and cost nothing


@pytest.mark.asyncio
async def test_an_ambiguous_line_is_settled_by_the_classifier(monkeypatch):
    rt = _mention_rt(monkeypatch, classifier="YES")
    addressed.note_bot_reply(CHAT)
    msg = _msg(text="why would you even say that")
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    rt.openai.cheap_completion.assert_awaited_once()
    rt.openai.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_reply_reopens_the_window_for_the_next_follow_up(monkeypatch):
    """His own reply is what keeps the conversation going."""
    rt = _mention_rt(monkeypatch)
    msg = _msg(text="dale is the grill safe")   # not "propane" — that is an automod trigger
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    assert addressed.in_conversation(CHAT)


@pytest.mark.asyncio
async def test_reply_policy_stays_strict(monkeypatch):
    """`reply` is the operator saying 'only when replied to'."""
    rt = _mention_rt(monkeypatch)
    rt.chats.get_config.return_value.response_policy = "reply"
    addressed.note_bot_reply(CHAT)
    msg = _msg(text="why?")
    msg.answer = AsyncMock()
    await _handler(rt)(msg)
    rt.openai.chat.assert_not_awaited()
