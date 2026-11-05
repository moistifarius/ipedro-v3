"""Kinds of poll (quiz, public votes, several answers, a timer) and the bot
asking back what kind, or what it is about.

What has to hold: a kind someone names is applied in code whatever the model
says; a topic that merely contains the word ("public transport", "pub quiz")
is not a kind; Telegram's rules for quizzes and timers are met (and a quiz
carries both the old and the new name for its right answer, since the
Bot API changed it under the aiogram this runs on); a question he asks is
answered by the asker only, in the right place, once, before it goes stale;
and the follow-up never asks again.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.types import ForceReply

from ipedro import capabilities, poll_maker, prompts, ratelimit
from ipedro.poll_maker import (
    ASK_KIND, ASK_TOPIC, ASK_WHEN, DROPPED, NO_ANSWER_MARKED, NO_QUIZ_ANSWER,
    NOTHING, Ask, Kind, Material, Pending, PollDraft, apply_kind, build_prompt,
    clean_draft, continue_poll, detect_poll_ask, draft_poll, make_poll,
    parse_draft, parse_kind, parse_reply, piped, split_kind_prefix, take_pending,
)
from tests.test_poll_maker import (
    GOOD, JPEG, _chat_handler, _chat_rt, _cmd_rt, _in_chat, _msg, _photo,
    _poll_handler, _quoted, _rt,
)

QUIZ = {"question": "Capital of France?", "options": ["London", "Paris", "Rome"],
        "correct": [1], "explanation": "Paris has been the capital for centuries."}


class FakeBot(SimpleNamespace):
    """A bot that can also be called with a method object, as aiogram's can."""

    async def __call__(self, method):
        self.methods.append(method)
        return SimpleNamespace(message_id=500)


def _with_calls(rt):
    rt.bot = FakeBot(**vars(rt.bot), methods=[])
    return rt


def _quizzer(**kw):
    return _with_calls(_rt(**kw))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    ratelimit.LIMITER.reset()
    poll_maker._PENDING.clear()
    monkeypatch.setattr(poll_maker, "track", Mock())
    yield
    ratelimit.LIMITER.reset()
    poll_maker._PENDING.clear()


# ── reading the kind ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expect", [
    ("make it a quiz", dict(quiz=True)),
    ("a quiz poll about capitals", dict(quiz=True)),
    ("quiz-style please", dict(quiz=True)),
    ("with a correct answer", dict(quiz=True)),
    ("one right answer", dict(quiz=True)),
    ("trivia poll", dict(quiz=True)),
    ("who wins, quiz", dict(quiz=True)),
    ("make it public", dict(anonymous=False)),
    ("a public poll", dict(anonymous=False)),
    ("non-anonymous", dict(anonymous=False)),
    ("not anonymous", dict(anonymous=False)),
    ("show who voted", dict(anonymous=False)),
    ("lunch, public, closes in 2 hours", dict(anonymous=False, open_seconds=7200)),
    ("pick all that apply", dict(multiple=True)),
    ("select all", dict(multiple=True)),
    ("multiple answers", dict(multiple=True)),
    ("several selections", dict(multiple=True)),
    ("pick only one", dict(multiple=False)),
    ("single answer", dict(multiple=False)),
    ("closes in 10 minutes", dict(open_seconds=600)),
    ("this poll ends after 2 hours", dict(open_seconds=7200)),
    ("timer of 1 day", dict(open_seconds=86400)),
    ("closes in half an hour", dict(open_seconds=1800)),
    ("it expires in an hour", dict(open_seconds=3600)),
    ("a 5 minute poll", dict(open_seconds=300)),
    ("timer of thirty seconds", dict(open_seconds=30)),
    ("closes in 2 weeks", dict(open_seconds=1_209_600)),
    ("closes in 1 second", dict(open_seconds=5)),            # Telegram's floor
    ("ask me what kind", dict(ask_kind=True)),
    ("what kind of poll do you want", dict(ask_kind=True)),
    ("a timed poll", dict(ask_when=True)),
])
def test_a_kind_that_is_asked_for_is_read(text, expect):
    got = parse_kind(text)
    for field_name, value in expect.items():
        assert getattr(got, field_name) == value, (text, got)


@pytest.mark.parametrize("text", [
    "public transport", "should the public park close", "the pub quiz night",
    "quiz night on friday", "a 5 minute walk", "meeting for 10 minutes",
    "multiple choice", "lunch and public transport", "what is the right answer to life",
    "who is publicly funded", "several of us", "many thanks",
])
def test_a_topic_that_merely_contains_the_word_is_not_a_kind(text):
    assert parse_kind(text) == Kind()


def test_a_timer_past_what_telegram_allows_is_clamped_and_says_so():
    got = parse_kind("closes in 90 days")
    assert got.open_seconds == poll_maker.MAX_OPEN_SECONDS and got.clamped


@pytest.mark.parametrize("text, expect", [
    ("quiz", dict(quiz=True)), ("public", dict(anonymous=False)),
    ("a quiz, public", dict(quiz=True, anonymous=False)),
    ("make a quiz poll", dict(quiz=True)), ("10 minutes", dict(open_seconds=600)),
    ("2 hours", dict(open_seconds=7200)), ("pick several", dict(multiple=True)),
    ("multiple answers", dict(multiple=True)),
])
def test_words_that_are_only_about_the_kind_are_read_loosely(text, expect):
    got = parse_kind(text, loose=True)
    for field_name, value in expect.items():
        assert getattr(got, field_name) == value


def test_multiple_choice_is_not_several_answers_even_loosely():
    assert parse_kind("make a multiple choice poll", loose=True).multiple is None


def test_kinds_merge_with_the_later_word_winning():
    a = Kind(quiz=True, open_seconds=60)
    b = Kind(anonymous=False, open_seconds=600, multiple=True)
    got = a.merged(b)
    assert got.quiz and not got.anonymous and got.open_seconds == 600 and got.multiple
    assert Kind(ask_when=True).merged(Kind(open_seconds=60)).ask_when is False


@pytest.mark.parametrize("text, prefix, rest", [
    ("quiz, public: Capital? | London | Paris* | Rome", "quiz, public", "Capital? | London | Paris* | Rome"),
    ("closes in 10 minutes: lunch?", "closes in 10 minutes", "lunch?"),
    ("quiz: capitals", "quiz", "capitals"),
    ("Timed 5 minutes: x", "Timed 5 minutes", "x"),
    ("regular: lunch", "regular", "lunch"),
])
def test_a_kind_can_lead_a_request(text, prefix, rest):
    assert split_kind_prefix(text) == (prefix, rest)


@pytest.mark.parametrize("text", [
    "what time: now | later | never", "lunch: pizza | tacos | sushi",
    "public transport: good or bad", "quiz night: yes | no | maybe",
    "quiz and the rest of the sentence goes on and on: x", "no colon here",
    ":", "", "10: tacos", "in: out",
])
def test_a_colon_in_a_question_is_left_alone(text):
    assert split_kind_prefix(text) == ("", text)


# ── the ask names the kind ──────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "make a quiz poll", "make us a quick public poll", "create a timed poll",
    "make a multiple choice poll", "start an anonymous poll", "make a trivia poll",
    "make a regular poll", "make us a quick little public quiz poll",
    "dale make a yes/no poll",
])
def test_a_kind_adjective_still_makes_it_an_ask(text):
    assert detect_poll_ask(text) is not None


def test_the_asking_words_are_handed_on_with_what_follows():
    found = detect_poll_ask("dale make a public quiz poll about capitals")
    assert found.rest == "capitals" and found.ask.lower() == "a public quiz"
    assert detect_poll_ask("make a quiz") is None            # a quiz is not a poll
    assert detect_poll_ask("make a quiz night poll") is None  # nor is "night" a kind


# ── what the model returns for a quiz ───────────────────────────────────────

def test_the_right_answers_follow_their_options_through_cleaning():
    d = clean_draft({"question": "Q", "options": ["1. a", "2. b", "3. c"], "correct": [1]})
    assert d.options == ["a", "b", "c"] and d.correct == (1,)
    d = clean_draft({"question": "Q", "options": ["a", "", "b", "A", "c"], "correct": [4]})
    assert d.options == ["a", "b", "c"] and d.correct == (2,)       # blanks and dupes shift it
    d = clean_draft({"question": "Q", "options": ["a", "b", "A"], "correct": [2]})
    assert d.correct == (0,)                                        # a duplicate of the right one


@pytest.mark.parametrize("correct, expect", [
    (1, (1,)), ([0, 2], (0, 2)), ("1", (1,)), (["2", 0], (0, 2)), ([1, 1], (1,)),
    ([5], ()), ([-1], ()), (None, ()), ("x", ()), ([True], ()), ([1.5], ()),
])
def test_what_counts_as_a_right_answer(correct, expect):
    d = clean_draft({"question": "Q", "options": ["a", "b", "c"], "correct": correct})
    assert d.correct == expect


def test_an_answer_cut_off_by_the_option_limit_is_gone():
    d = clean_draft({"question": "Q", "options": [f"o{i}" for i in range(14)], "correct": [12]})
    assert len(d.options) == 10 and d.correct == ()


def test_the_explanation_is_one_short_line():
    d = clean_draft({"question": "Q", "options": ["a", "b"], "correct": [0],
                     "explanation": "line one\n\nline two\n" + "x" * 400})
    assert "\n" not in d.explanation and len(d.explanation) <= 200
    assert clean_draft({"question": "Q", "options": ["a", "b"], "explanation": "  "}).explanation is None


# ── applying the kind ───────────────────────────────────────────────────────

def _draft(**kw):
    base = dict(question="Q", options=["a", "b", "c"], correct=(1,), explanation="why")
    base.update(kw)
    return PollDraft(**base)


def test_a_quiz_keeps_its_right_answer_and_its_why():
    got = apply_kind(_draft(), Kind(quiz=True))
    assert got.quiz and got.correct == (1,) and got.explanation == "why" and not got.multiple


def test_a_regular_poll_drops_what_only_a_quiz_has():
    got = apply_kind(_draft(), Kind())
    assert not got.quiz and got.correct == () and got.explanation is None


def test_a_quiz_with_no_right_answer_is_not_a_poll():
    assert apply_kind(_draft(correct=()), Kind(quiz=True)) is None


def test_several_right_answers_make_it_a_several_answers_quiz_unless_told_one():
    two = _draft(correct=(0, 2))
    assert apply_kind(two, Kind(quiz=True)).multiple is True
    one = apply_kind(two, Kind(quiz=True, multiple=False))
    assert one.correct == (0,) and one.multiple is False


def test_what_the_person_said_beats_what_the_model_guessed():
    assert apply_kind(_draft(multiple=True), Kind(multiple=False)).multiple is False
    assert apply_kind(_draft(multiple=False), Kind(multiple=True)).multiple is True
    assert apply_kind(_draft(multiple=True), Kind()).multiple is True       # nothing said
    got = apply_kind(_draft(), Kind(anonymous=False, open_seconds=600))
    assert got.anonymous is False and got.open_seconds == 600


def test_a_timer_past_the_limit_is_noted():
    got = apply_kind(_draft(), Kind(open_seconds=poll_maker.MAX_OPEN_SECONDS, clamped=True))
    assert "30 days" in got.note
    assert apply_kind(_draft(), Kind(open_seconds=60)).note is None


# ── typed quizzes ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("typed, correct", [
    ("Capital? | London | Paris* | Rome", (1,)),
    ("Capital? | London | Paris * | Rome", (1,)),
    ("Capital? | London | ✓ Paris | Rome", (1,)),
    ("Capital? | London | Paris ✅ | Rome", (1,)),
    ("Capital? | London* | Paris | Rome*", (0, 2)),
    ("Capital? | London | Paris | Rome", ()),
    ("Capital? | * | Paris | Rome", ()),                      # a lone star is an option
])
def test_the_right_answer_of_a_typed_quiz_is_the_marked_one(typed, correct):
    d = piped(typed, quiz=True)
    assert d.correct == correct
    assert all("*" not in o and "✓" not in o and "✅" not in o for o in d.options if o != "*")


def test_marks_mean_nothing_in_a_regular_typed_poll():
    d = piped("Rate it | 5* | 4 | 3", quiz=False)
    assert d.options == ["5*", "4", "3"] and d.correct == ()


# ── what goes into history ──────────────────────────────────────────────────

def test_the_history_line_says_the_kind_and_marks_the_right_answer():
    got = apply_kind(_draft(), Kind(quiz=True, anonymous=False, open_seconds=600)).summary()
    assert got == "[made a quiz poll] Q — a | b ✓ | c (public votes, closes in 10 minutes)"
    assert apply_kind(_draft(), Kind(open_seconds=3600)).summary().endswith("(closes in 1 hour)")
    assert apply_kind(_draft(), Kind(open_seconds=90)).summary().endswith("(closes in 90 seconds)")
    plain = apply_kind(_draft(), Kind(multiple=True)).summary()
    assert plain == "[made a poll] Q — a | b | c (several answers allowed)"
    assert apply_kind(_draft(), Kind()).summary() == "[made a poll] Q — a | b | c"


# ── what the drafter is told ────────────────────────────────────────────────

def test_the_prompt_asks_for_a_quiz_only_when_one_was_asked_for():
    plain = build_prompt("x", None, Kind())
    quiz = build_prompt("x", None, Kind(quiz=True))
    assert "QUIZ" not in plain and '"correct"' in quiz and "explanation" in quiz
    assert "a guess" not in quiz                                   # no ask rule slips in


def test_the_prompt_lets_it_ask_once_or_never():
    assert '{"ask":' in build_prompt("x", None, Kind(), may_ask=True)
    never = build_prompt("x", None, Kind(), may_ask=False)
    assert '{"ask":' not in never and "Never ask a question" in never
    assert '"which one is the right answer?"' in build_prompt("x", None, Kind(quiz=True), may_ask=True)
    assert '"which one is the right answer?"' not in build_prompt("x", None, Kind(quiz=True))


def test_the_prompt_says_one_answer_or_several_when_that_was_said():
    assert "Several answers" in build_prompt("x", None, Kind(multiple=True))
    assert "Only one answer" in build_prompt("x", None, Kind(multiple=False))
    quiet = build_prompt("x", None, Kind())
    assert "Several answers can be picked" not in quiet and "Only one answer" not in quiet


def test_the_prompt_still_formats_with_hostile_braces_in_the_request():
    assert "{ask}" in build_prompt("{ask} {kind} {{", None, Kind(quiz=True), may_ask=True)


def test_an_ask_comes_back_only_when_it_may():
    reply = json.dumps({"ask": "Which decision?"})
    assert parse_reply(reply) == Ask("Which decision?")
    assert parse_draft(reply) is None
    assert parse_reply(json.dumps({"ask": "  "})) is None
    assert parse_reply(json.dumps({"ask": "q", "question": "Q", "options": ["a", "b"]})).options == ["a", "b"]


@pytest.mark.asyncio
async def test_the_drafter_gives_an_ask_to_those_who_can_take_it():
    rt = _rt(reply={"ask": "which one?"})
    assert await draft_poll(rt.openai, request="x", material=None, may_ask=True) == Ask("which one?")
    assert await draft_poll(rt.openai, request="x", material=None, may_ask=False) is None


# ── posting each kind ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_public_timed_poll_goes_out_public_and_timed():
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("poll"), "lunch", ask_text="make a public poll that closes in 10 minutes")
    sent = rt.bot.send_poll.await_args.kwargs
    assert sent["is_anonymous"] is False and sent["open_period"] == 600


@pytest.mark.asyncio
async def test_a_regular_poll_is_anonymous_and_untimed_by_default():
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("poll"), "lunch")
    sent = rt.bot.send_poll.await_args.kwargs
    assert sent["is_anonymous"] is True and sent["open_period"] is None


@pytest.mark.asyncio
async def test_a_quiz_is_sent_as_a_method_carrying_both_names_for_its_answer():
    rt = _quizzer(reply=QUIZ)
    out = await make_poll(rt, _msg("quiz"), "capitals", ask_text="make a quiz poll")
    (method,) = rt.bot.methods
    assert type(method).__name__ == "SendPoll"
    sent = method.model_dump(exclude_none=True)
    assert sent["type"] == "quiz" and sent["options"] == ["London", "Paris", "Rome"]
    assert sent["correct_option_id"] == 1 and sent["correct_option_ids"] == [1]
    assert sent["explanation"].startswith("Paris has been")
    assert "allows_multiple_answers" not in sent and sent["is_anonymous"] is True
    assert sent["reply_parameters"]["message_id"] == 100
    rt.bot.send_poll.assert_not_awaited()
    assert out.startswith("[made a quiz poll] Capital of France? — London | Paris ✓ | Rome")


@pytest.mark.asyncio
async def test_a_quiz_with_several_right_answers_allows_several_and_names_only_the_new_field():
    rt = _quizzer(reply={**QUIZ, "correct": [0, 2]})
    await make_poll(rt, _msg("quiz"), "x", ask_text="make a quiz poll, multiple answers")
    sent = rt.bot.methods[0].model_dump(exclude_none=True)
    assert sent["allows_multiple_answers"] is True
    assert sent["correct_option_ids"] == [0, 2] and "correct_option_id" not in sent


@pytest.mark.asyncio
async def test_a_typed_quiz_with_its_answer_marked_is_posted_with_no_model():
    rt = _quizzer()
    out = await make_poll(rt, _msg("x"), "Capital? | London | Paris* | Rome", ask_text="make a quiz poll")
    sent = rt.bot.methods[0].model_dump(exclude_none=True)
    assert sent["correct_option_ids"] == [1] and sent["options"] == ["London", "Paris", "Rome"]
    assert "explanation" not in sent and "Paris ✓" in out
    rt.openai.cheap_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_typed_quiz_with_no_answer_marked_says_how_to_mark_it():
    rt = _quizzer()
    msg = _msg("x")
    assert await make_poll(rt, msg, "Capital? | London | Paris | Rome", ask_text="quiz") == NO_ANSWER_MARKED
    assert not rt.bot.methods and msg.reply.await_args.args == (NO_ANSWER_MARKED,)


@pytest.mark.asyncio
async def test_a_quiz_the_model_cant_answer_says_so():
    rt = _quizzer(reply={"question": "Q", "options": ["a", "b"]})
    msg = _msg("x")
    assert await make_poll(rt, msg, "capitals", ask_text="make a quiz poll", may_ask=False) == NO_QUIZ_ANSWER
    assert not rt.bot.methods


@pytest.mark.asyncio
async def test_a_regular_poll_ignores_the_right_answer_a_model_volunteers():
    rt = _rt(reply=QUIZ)
    await make_poll(rt, _msg("x"), "capitals")
    sent = rt.bot.send_poll.await_args.kwargs
    assert "correct_option_id" not in sent and "explanation" not in sent


@pytest.mark.asyncio
async def test_a_timer_longer_than_telegram_allows_is_said_after_the_poll():
    rt = _rt(reply=GOOD)
    msg = _msg("x")
    await make_poll(rt, msg, "lunch", ask_text="make a poll that closes in 90 days")
    assert rt.bot.send_poll.await_args.kwargs["open_period"] == poll_maker.MAX_OPEN_SECONDS
    assert "30 days" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_a_poll_typed_whole_can_still_be_public_and_timed():
    rt = _rt()
    await make_poll(rt, _msg("x"), "where | pizza | tacos", ask_text="make a public poll that closes in 5 minutes")
    sent = rt.bot.send_poll.await_args.kwargs
    assert sent["is_anonymous"] is False and sent["open_period"] == 300
    rt.openai.cheap_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_question_of_a_typed_poll_is_never_read_for_a_kind():
    rt = _rt()
    await make_poll(rt, _msg("x"), "Is public transport a quiz? | yes | no")
    sent = rt.bot.send_poll.await_args.kwargs
    assert sent["is_anonymous"] is True and "correct_option_ids" not in sent


@pytest.mark.asyncio
async def test_a_kind_prefix_on_the_request_is_read_and_taken_off():
    rt = _quizzer()
    await make_poll(rt, _msg("x"), "quiz, public: Capital? | London | Paris* | Rome")
    sent = rt.bot.methods[0].model_dump(exclude_none=True)
    assert sent["question"] == "Capital?" and sent["is_anonymous"] is False


# ── over the real aiogram ───────────────────────────────────────────────────

def _capture_bot():
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Chat, Message

    class Capture(BaseSession):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def close(self):
            pass

        async def stream_content(self, *a, **k):
            pass

        async def make_request(self, bot, method, timeout=None):
            self.calls.append(method)
            return Message(message_id=77, date=datetime.now(), chat=Chat(id=42, type="group"))

    session = Capture()
    return Bot("123:abc", session=session, default=DefaultBotProperties(parse_mode=None)), session


@pytest.mark.asyncio
async def test_a_quiz_is_a_well_formed_telegram_request():
    bot, session = _capture_bot()
    rt = SimpleNamespace(bot=bot)
    draft = apply_kind(PollDraft("Capital?", ["London", "Paris"], correct=(1,), explanation="why"),
                       Kind(quiz=True, anonymous=False, open_seconds=600))
    await poll_maker._post(rt, _msg("x"), draft)
    dumped = session.calls[0].model_dump(exclude_none=True)
    assert dumped["type"] == "quiz" and dumped["correct_option_id"] == 1
    assert dumped["correct_option_ids"] == [1] and dumped["explanation"] == "why"
    assert dumped["is_anonymous"] is False and dumped["open_period"] == 600
    assert dumped["reply_parameters"]["allow_sending_without_reply"] is True


@pytest.mark.asyncio
async def test_a_timed_public_poll_is_a_well_formed_telegram_request():
    bot, session = _capture_bot()
    rt = SimpleNamespace(bot=bot)
    await poll_maker._post(rt, _msg("x"), PollDraft("Q", ["a", "b"], anonymous=False, open_seconds=3600))
    dumped = session.calls[0].model_dump(exclude_none=True)
    assert dumped["is_anonymous"] is False and dumped["open_period"] == 3600
    assert "type" not in dumped and "correct_option_id" not in dumped


# ── asking back ─────────────────────────────────────────────────────────────

def _answer(text, *, reply_to_id=101, user_id=7, chat_type="group", chat_id=42):
    quoted = SimpleNamespace(message_id=reply_to_id) if reply_to_id else None
    return _msg(text, reply_to=quoted, user_id=user_id, chat_type=chat_type, chat_id=chat_id)


@pytest.mark.asyncio
async def test_with_nothing_to_poll_he_asks_what_it_is_about_as_a_force_reply():
    rt = _rt(reply=GOOD)
    msg = _msg("make a poll")
    assert await make_poll(rt, msg, "") == ASK_TOPIC
    markup = msg.reply.await_args.kwargs["reply_markup"]
    assert isinstance(markup, ForceReply) and markup.selective and markup.force_reply
    assert len(markup.input_field_placeholder) <= 64
    assert "disable_notification" not in msg.reply.await_args.kwargs      # it wants an answer
    rt.openai.cheap_completion.assert_not_awaited()
    assert ratelimit.LIMITER._hits == {}                                   # asking is free


@pytest.mark.asyncio
async def test_the_answer_finishes_the_poll_with_the_kind_in_it():
    rt = _quizzer(reply=QUIZ)
    await make_poll(rt, _msg("make a poll"), "")
    ans = _answer("quiz poll about capitals")
    pending = take_pending(ans)
    assert pending is not None and pending.mode == "topic"
    out = await continue_poll(rt, ans, pending, "quiz poll about capitals")
    assert out.startswith("[made a quiz poll]")
    assert "capitals" in rt.openai.cheap_completion.await_args.args[0]
    assert take_pending(ans) is None                                       # used once


@pytest.mark.asyncio
async def test_the_answer_can_be_a_poll_typed_whole():
    rt = _rt()
    await make_poll(rt, _msg("make a poll"), "")
    ans = _answer("where | pizza | tacos")
    out = await continue_poll(rt, ans, take_pending(ans), "where | pizza | tacos")
    assert out.startswith("[made a poll] where")
    rt.openai.cheap_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_in_a_group_only_a_reply_to_the_question_is_the_answer():
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("make a poll"), "")
    assert take_pending(_answer("pizza", reply_to_id=None)) is None        # just chatting
    assert take_pending(_answer("pizza", reply_to_id=999)) is None         # a reply to something else
    assert take_pending(_answer("pizza", user_id=8)) is None               # someone else
    assert take_pending(_answer("pizza", chat_id=43)) is None              # another chat
    assert take_pending(_answer("pizza")) is not None                      # the asker, replying


@pytest.mark.asyncio
async def test_in_a_private_chat_the_next_message_is_the_answer():
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("make a poll", chat_type="private"), "")
    assert take_pending(_answer("pizza", reply_to_id=None, chat_type="private")) is not None


@pytest.mark.asyncio
async def test_a_stale_question_is_not_answered(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(poll_maker, "_now", lambda: clock["t"])
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("make a poll"), "")
    clock["t"] += poll_maker._PENDING_TTL + 1
    assert take_pending(_answer("pizza")) is None
    assert poll_maker._PENDING == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["never mind", "nvm", "cancel", "forget it", "No", "nope.", "stop!"])
async def test_a_cancel_drops_it_without_a_model_call(word):
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("make a poll"), "")
    ans = _answer(word)
    assert await continue_poll(rt, ans, take_pending(ans), word) == DROPPED
    rt.openai.cheap_completion.assert_not_awaited()
    assert ans.reply.await_args.args == (DROPPED,)


@pytest.mark.asyncio
async def test_an_answer_that_only_starts_like_a_cancel_is_an_answer():
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("make a poll"), "")
    ans = _answer("no more pizza, tacos instead")
    out = await continue_poll(rt, ans, take_pending(ans), "no more pizza, tacos instead")
    assert out.startswith("[made a poll]")


@pytest.mark.asyncio
async def test_asking_what_kind_waits_and_the_answer_sets_it():
    rt = _quizzer(reply=QUIZ)
    msg = _msg("poll this", reply_to=_msg("capitals of europe"))
    assert await make_poll(rt, msg, "", ask_text="make a poll, ask me what kind") == ASK_KIND
    rt.openai.cheap_completion.assert_not_awaited()
    ans = _answer("a quiz, public")
    out = await continue_poll(rt, ans, take_pending(ans), "a quiz, public")
    sent = rt.bot.methods[0].model_dump(exclude_none=True)
    assert sent["type"] == "quiz" and sent["is_anonymous"] is False
    assert "capitals of europe" in rt.openai.cheap_completion.await_args.args[0]
    assert out.startswith("[made a quiz poll]")


@pytest.mark.asyncio
async def test_a_regular_answer_to_what_kind_gives_a_regular_poll():
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("poll"), "lunch", ask_text="make a poll, ask me what kind")
    ans = _answer("just a regular one")
    out = await continue_poll(rt, ans, take_pending(ans), "just a regular one")
    assert out.startswith("[made a poll]") and rt.bot.send_poll.await_args.kwargs["is_anonymous"] is True


@pytest.mark.asyncio
async def test_a_timed_poll_with_no_length_asks_how_long_and_the_answer_sets_it():
    rt = _rt(reply=GOOD)
    assert await make_poll(rt, _msg("x"), "lunch", ask_text="make a timed poll") == ASK_WHEN
    ans = _answer("2 hours")
    await continue_poll(rt, ans, take_pending(ans), "2 hours")
    assert rt.bot.send_poll.await_args.kwargs["open_period"] == 7200


@pytest.mark.asyncio
async def test_an_answer_that_is_no_length_gives_an_untimed_poll_not_another_question():
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("x"), "lunch", ask_text="make a timed poll")
    ans = _answer("whenever")
    out = await continue_poll(rt, ans, take_pending(ans), "whenever")
    assert out.startswith("[made a poll]") and rt.bot.send_poll.await_args.kwargs["open_period"] is None
    assert poll_maker._PENDING == {}


@pytest.mark.asyncio
async def test_the_drafter_can_ask_one_thing_and_the_answer_is_added_to_the_request():
    rt = _rt(reply={"ask": "Which of the two decisions?"})
    msg = _msg("poll this", reply_to=_msg("we could move the date or change the venue"))
    assert await make_poll(rt, msg, "") == "Which of the two decisions?"
    assert "disable_notification" not in msg.reply.await_args.kwargs
    assert isinstance(msg.reply.await_args.kwargs["reply_markup"], ForceReply)
    rt.openai.cheap_completion.return_value = json.dumps(GOOD)
    ans = _answer("the venue")
    await continue_poll(rt, ans, take_pending(ans), "the venue")
    prompt = rt.openai.cheap_completion.await_args.args[0]
    assert "the venue" in prompt and "we could move the date" in prompt


@pytest.mark.asyncio
async def test_after_one_question_he_never_asks_again():
    rt = _rt(reply={"ask": "and what else?"})
    await make_poll(rt, _msg("poll"), "lunch")
    ans = _answer("x")
    assert await continue_poll(rt, ans, take_pending(ans), "x") == NOTHING
    assert poll_maker._PENDING == {}
    assert '{"ask":' not in rt.openai.cheap_completion.await_args.args[0]


@pytest.mark.asyncio
async def test_a_follow_up_that_still_says_ask_me_does_not_ask_again():
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("poll"), "lunch", ask_text="make a poll, ask me what kind")
    ans = _answer("ask me")
    out = await continue_poll(rt, ans, take_pending(ans), "ask me")
    assert out.startswith("[made a poll]") and poll_maker._PENDING == {}


@pytest.mark.asyncio
async def test_drafting_after_a_question_is_still_limited():
    rt = _rt(reply=GOOD)
    for _ in range(ratelimit.LIMITS["poll"][0]):
        ratelimit.wait_for("poll", 7, 42)
    await make_poll(rt, _msg("poll"), "", ask_text="")
    ans = _answer("lunch")
    assert await continue_poll(rt, ans, take_pending(ans), "lunch") is None
    assert "polls for the hour" in ans.reply.await_args.args[0]
    rt.openai.cheap_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_second_ask_replaces_the_first():
    rt = _rt(reply=GOOD)
    first, second = _msg("make a poll"), _msg("make a poll")
    first.reply = AsyncMock(return_value=SimpleNamespace(message_id=201))
    second.reply = AsyncMock(return_value=SimpleNamespace(message_id=202))
    await make_poll(rt, first, "")
    await make_poll(rt, second, "")
    assert take_pending(_answer("x", reply_to_id=201)) is None
    assert take_pending(_answer("x", reply_to_id=202)) is not None


@pytest.mark.asyncio
async def test_a_question_that_could_not_be_sent_waits_for_nothing():
    rt = _rt(reply=GOOD)
    msg = _msg("make a poll")
    msg.reply = AsyncMock(side_effect=RuntimeError("gone"))
    assert await make_poll(rt, msg, "") is None
    assert poll_maker._PENDING == {}


@pytest.mark.asyncio
async def test_nobody_to_wait_for_gets_the_plain_hint():
    rt = _rt(reply=GOOD)
    msg = _msg("make a poll")
    msg.from_user = None
    assert await make_poll(rt, msg, "") == poll_maker.HINT
    assert poll_maker._PENDING == {}


def test_waiting_questions_are_bounded_and_few_hold_a_picture(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(poll_maker, "_now", lambda: clock["t"])
    for i in range(poll_maker._PENDING_MAX + 40):
        clock["t"] += 0.01
        poll_maker._remember((1, i), Pending("topic", "", "", None, i, clock["t"]))
    assert len(poll_maker._PENDING) == poll_maker._PENDING_MAX
    assert (1, 0) not in poll_maker._PENDING                                 # the oldest went
    poll_maker._PENDING.clear()
    big = Material(image=JPEG, media_type="image/jpeg")
    for i in range(poll_maker._PENDING_WITH_IMAGE_MAX + 5):
        clock["t"] += 0.01
        poll_maker._remember((2, i), Pending("kind", "x", "", big, i, clock["t"]))
    held = [k for k, p in poll_maker._PENDING.items() if p.material is not None]
    assert len(held) == poll_maker._PENDING_WITH_IMAGE_MAX and (2, 0) not in held


def test_old_questions_go_when_a_new_one_comes(monkeypatch):
    poll_maker._remember((1, 1), Pending("topic", "", "", None, 1, 0.0))
    poll_maker._remember((1, 2), Pending("topic", "", "", None, 2, poll_maker._PENDING_TTL + 5))
    assert list(poll_maker._PENDING) == [(1, 2)]


# ── in the chat ─────────────────────────────────────────────────────────────

def _chatty(monkeypatch, **kw):
    return _with_calls(_chat_rt(monkeypatch, **kw))


@pytest.mark.asyncio
async def test_a_bare_ask_in_the_chat_is_a_question_and_the_reply_makes_the_poll(monkeypatch):
    rt = _chatty(monkeypatch, reply=QUIZ)
    asked = _in_chat("dale make a poll")
    await _chat_handler(rt)(asked)
    assert asked.reply.await_args.args[0] == ASK_TOPIC
    rt.openai.chat.assert_not_awaited()
    answer = _in_chat("a quiz about capitals", reply_to=SimpleNamespace(
        message_id=101, from_user=SimpleNamespace(id=1, is_bot=True)))
    await _chat_handler(rt)(answer)
    assert rt.bot.methods and rt.bot.methods[0].type == "quiz"
    rt.openai.chat.assert_not_awaited()
    said = [c.kwargs["content"][:14] for c in rt.memory.record_message.await_args_list
            if c.kwargs["role"] == "assistant"]
    assert said == ["What's the pol", "[made a quiz p"]


@pytest.mark.asyncio
async def test_a_message_that_is_not_a_reply_to_the_question_carries_on_as_chat(monkeypatch):
    rt = _chatty(monkeypatch)
    await _chat_handler(rt)(_in_chat("dale make a poll"))
    await _chat_handler(rt)(_in_chat("dale what do you think of pizza"))
    rt.openai.chat.assert_awaited_once()
    assert poll_maker._PENDING                                              # still waiting


@pytest.mark.asyncio
async def test_the_answer_is_taken_before_the_funny_bits_can_have_it(monkeypatch):
    rt = _chatty(monkeypatch, reply=GOOD)
    await _chat_handler(rt)(_in_chat("dale make a poll"))
    answer = _in_chat("propane or based", reply_to=SimpleNamespace(
        message_id=101, from_user=SimpleNamespace(id=1, is_bot=True)))
    await _chat_handler(rt)(answer)
    rt.bot.send_poll.assert_awaited_once()
    assert "propane or based" in rt.openai.cheap_completion.await_args.args[0]


@pytest.mark.asyncio
async def test_a_dm_needs_no_reply(monkeypatch):
    rt = _chatty(monkeypatch, reply=GOOD)
    await _chat_handler(rt)(_in_chat("make a poll", chat_type="private"))
    await _chat_handler(rt)(_in_chat("pizza or tacos", chat_type="private"))
    rt.bot.send_poll.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_command_is_never_taken_for_an_answer(monkeypatch):
    rt = _chatty(monkeypatch, reply=GOOD)
    await _chat_handler(rt)(_in_chat("make a poll", chat_type="private"))
    cmd = _in_chat("/poll lunch | a | b", chat_type="private")
    await _chat_handler(rt)(cmd)
    rt.bot.send_poll.assert_not_awaited()
    assert poll_maker._PENDING


@pytest.mark.asyncio
async def test_the_ask_phrase_carries_its_kind_through_the_chat(monkeypatch):
    rt = _chatty(monkeypatch, reply=GOOD)
    await _chat_handler(rt)(_in_chat("dale make a public poll about lunch that closes in 5 minutes"))
    sent = rt.bot.send_poll.await_args.kwargs
    assert sent["is_anonymous"] is False and sent["open_period"] == 300


# ── /poll ───────────────────────────────────────────────────────────────────

def _quiz_cmd_rt(**kw):
    rt, cfg = _cmd_rt(**kw)
    return _with_calls(rt), cfg


@pytest.mark.asyncio
async def test_the_command_takes_a_kind_prefix_on_a_typed_poll():
    rt, _ = _quiz_cmd_rt()
    await _poll_handler(rt)(_msg("/poll quiz, public: Capital? | London | Paris* | Rome"))
    sent = rt.bot.methods[0].model_dump(exclude_none=True)
    assert sent["type"] == "quiz" and sent["is_anonymous"] is False and sent["correct_option_ids"] == [1]


@pytest.mark.asyncio
async def test_the_command_takes_a_kind_prefix_on_a_topic():
    rt, _ = _quiz_cmd_rt(reply=QUIZ)
    await _poll_handler(rt)(_msg("/poll quiz: capitals of europe"))
    assert rt.bot.methods[0].type == "quiz"
    assert "capitals of europe" in rt.openai.cheap_completion.await_args.args[0]


@pytest.mark.asyncio
async def test_the_command_alone_asks_what_it_is_about_and_waits_for_the_asker():
    rt, _ = _quiz_cmd_rt(reply=GOOD)
    await _poll_handler(rt)(_msg("/poll"))
    assert poll_maker._PENDING


@pytest.mark.asyncio
async def test_a_colon_in_a_typed_command_poll_is_still_the_legacy_path():
    rt, _ = _quiz_cmd_rt()
    await _poll_handler(rt)(_msg("/poll What time: now | later | never"))
    rt.bot.send_poll.assert_awaited_once_with(
        chat_id=42, question="What time: now", options=["later", "never"], is_anonymous=True)
    assert not rt.bot.methods


# ── what he is told he can do, and the docs ─────────────────────────────────

def test_the_brief_names_the_kinds_and_that_he_asks():
    brief = capabilities.capability_brief()
    assert "quiz" in brief and "public" in brief
    assert "never say one is up" in brief


def test_the_docs_describe_the_kinds_and_the_question():
    root = Path(__file__).resolve().parent.parent
    docs = " ".join((root / "docs" / "COMMANDS.md").read_text().split())
    for needle in ("quiz", "public", "pick all that apply", "closes in 10 minutes",
                   "Paris*", "30 days", "He asks", "replying to his question"):
        assert needle in docs, needle
    assert "quiz" in (root / "README.md").read_text()
