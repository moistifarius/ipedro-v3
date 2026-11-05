"""What the review of poll kinds, asking back and the GIF changes found, pinned.

On the first version: one 4000-digit message froze the bot for four seconds,
"and" and "ideas" were read as timers, "not public" made a public poll,
"ask me what kind" made a poll about those words, "turn the pub quiz into a
poll" made a quiz, "what kind of pizza" made him ask what kind of poll, a
timed poll never asked how long, a guns clip could go out unprompted, and a
dead link kept winning its tag.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ipedro import dale_gifs as dg
from ipedro import gif_reaction as gr
from ipedro import poll_maker, ratelimit
from ipedro.poll_maker import (
    ASK_KIND, ASK_TOPIC, ASK_TOPIC_WHEN, ASK_WHEN, DROPPED, NO_LENGTH, Kind,
    clean_request, continue_poll, detect_poll_ask, make_poll, parse_kind, piped,
    strip_ask_kind, take_pending,
)
from tests.test_gif_reaction import _DB, _chat_msg, _stocked
from tests.test_gif_reaction import _fresh as _gif_clock  # noqa: F401  (autouse clocks)
from tests.test_poll_kinds import (
    FakeBot, QUIZ, _answer, _poll_handler, _quiz_cmd_rt, _quizzer,
)
from tests.test_poll_maker import GOOD, _msg, _rt


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    ratelimit.LIMITER.reset()
    poll_maker._PENDING.clear()
    monkeypatch.setattr(poll_maker, "track", Mock())
    yield
    ratelimit.LIMITER.reset()
    poll_maker._PENDING.clear()


# ── a message cannot make the timer search slow ─────────────────────────────

HOSTILE = {
    "4000 digits": "9" * 4000,
    "digits and a unit": "9" * 4000 + " minutes",
    "digits, letter": "9" * 4000 + "x",
    "spaced digits": "1 " * 2000,
    "decimals": "1." * 2000,
    "units": "minutes " * 500,
    "a's": "a " * 2000,
    "cues": "closes in " * 400,
    "poll ends": "poll ends in " * 300,
    "half an": "half an " * 500,
    "and a half": " and a half" * 400,
    "stars": "x" + "*" * 4000,
    "star spaces": "x" + " *" * 2000 + " ",
    "commas": "lunch, " * 600,
    "listed kinds": ", public" * 500,
    "ticks": "✓" * 3000,
    "pipes": "q | " + " | ".join("o" for _ in range(1500)),
}


@pytest.mark.parametrize("text", HOSTILE.values(), ids=HOSTILE.keys())
@pytest.mark.parametrize("loose", [False, True])
def test_no_hostile_text_costs_more_than_a_blink_to_read_for_a_kind(text, loose):
    started = time.perf_counter()
    parse_kind(text, loose=loose)
    poll_maker.split_kind_prefix(text + ": x")
    poll_maker._correct_mark(text)
    strip_ask_kind(text)
    assert time.perf_counter() - started < 0.1


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["9" * 4000, "9" * 4000 + " minutes", "closes in " + "9" * 3000])
async def test_nor_end_to_end_through_make_poll_and_the_command(text):
    rt = _rt(reply=GOOD)
    started = time.perf_counter()
    await make_poll(rt, _msg("x"), text, ask_text="make a poll")
    assert time.perf_counter() - started < 0.5
    rt, _ = _quiz_cmd_rt(reply=GOOD)
    started = time.perf_counter()
    await _poll_handler(rt)(_msg("/poll " + text))
    assert time.perf_counter() - started < 0.5


# ── words are not timers ────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "turn these ideas into a poll", "regular, as usual", "public and several", "and", "as",
    "yeah", "was", "am", "phones and thread", "a s", "ideas", "an d", "many thanks",
    "one s", "the sheas", "Alas, and so", "pizza, tacos and ramen", "a m", "one h", "an h",
])
def test_letters_inside_ordinary_words_are_never_a_length(text):
    assert parse_kind(text, loose=True).open_seconds is None
    assert parse_kind(text).open_seconds is None


@pytest.mark.parametrize("text, seconds", [
    ("an hour", 3600), ("a day", 86400), ("half an hour", 1800), ("2d", 172800),
    ("30 secs", 30), ("10 minutes", 600), ("an hour and a half", 5400),
    ("2 hours 30 minutes", 9000), ("1 hour and 30 minutes", 5400),
    ("2 hours, 15 min", 8100), ("1.5 hours", 5400), ("twelve hours", 43200),
    ("a month", 2_592_000), ("1 week", 604800), ("3 mins", 180),
])
def test_a_length_in_an_answer_is_read_whole(text, seconds):
    assert parse_kind(text, loose=True).open_seconds == seconds


@pytest.mark.parametrize("text", [
    "closes in 99999999999999999999 days", "closes in 1e999 days", "closes in 𝟐 hours",
    "closes in ſix hours", "closes in İ hours", "closes in -5 minutes", "closes in 0 minutes",
    "closes in 0.0001 seconds", "closes in 10000000 days", "closes in 9" + "9" * 400 + " days",
])
def test_a_hostile_number_is_no_crash(text):
    got = parse_kind(text)
    assert got.open_seconds is None or (
        poll_maker.MIN_OPEN_SECONDS <= got.open_seconds <= poll_maker.MAX_OPEN_SECONDS)


def test_a_length_past_telegrams_is_clamped_and_noted_but_a_zero_is_not_a_timer():
    got = parse_kind("closes in 9999999 days")
    assert got.open_seconds == poll_maker.MAX_OPEN_SECONDS and got.clamped
    assert parse_kind("closes in 2 years").clamped
    assert parse_kind("closes in 0 minutes").open_seconds in (None, poll_maker.MIN_OPEN_SECONDS)


@pytest.mark.parametrize("text", [
    "which movie lasts 2 hours", "the last 3 days", "spend 2 hours", "closed for 2 weeks",
    "the movie ends in 5 minutes", "a 5 minute walk", "meeting for 10 minutes",
    "the store stays open for 2 hours", "it takes 20 minutes", "expires 2 hours ago",
])
def test_a_topic_with_a_length_in_it_is_not_a_timer(text):
    assert parse_kind(text).open_seconds is None


@pytest.mark.parametrize("text, seconds", [
    ("closes in 10 minutes", 600), ("poll ends in 2 hours", 7200), ("the poll closes after 1 day", 86400),
    ("it expires in an hour", 3600), ("a 10 minute poll", 600), ("timer of 2 hours", 7200),
    ("with a time limit of 30 minutes", 1800), ("auto-close in 5 minutes", 300),
])
def test_the_phrasings_that_do_name_a_poll_s_length_still_do(text, seconds):
    assert parse_kind(text).open_seconds == seconds


# ── kind words that are really a topic ──────────────────────────────────────

@pytest.mark.parametrize("text", [
    "turn the pub quiz into a poll", "turn this public statement into a poll",
    "turn these many ideas into a poll", "convert the trivia list into a poll",
    "put the public vote to a poll", "turn the timed test into a poll",
])
def test_the_thing_a_pointer_ask_turns_into_a_poll_is_not_the_kind(text):
    found = detect_poll_ask(text)
    assert found is not None and found.ask == "" and found.rest == ""


@pytest.mark.asyncio
async def test_so_replying_to_a_pub_quiz_gets_a_regular_poll():
    rt = _rt(reply=GOOD)
    quoted = _msg("the pub quiz is on friday, pizza or tacos?")
    msg = _msg("turn the pub quiz into a poll", reply_to=quoted)
    found = detect_poll_ask(msg.text)
    await make_poll(rt, msg, found.rest, ask_text=found.ask)
    sent = rt.bot.send_poll.await_args.kwargs
    assert sent["is_anonymous"] is True and "type" not in sent


@pytest.mark.parametrize("text", [
    "what kind of pizza do we want", "which type of car", "what sort of music",
    "ask me anything", "ask me about it later", "kind of poll", "the type of poll we need",
])
def test_a_topic_that_says_what_kind_is_not_a_request_to_be_asked(text):
    assert parse_kind(text).ask_kind is False


@pytest.mark.parametrize("text", [
    "ask me what kind", "ask me what kind of poll", "ask me", "what kind of poll?",
    "which type of poll do you want", "let me choose the kind", "ask us, please",
])
def test_these_do_ask_to_be_asked(text):
    assert parse_kind(text).ask_kind is True


@pytest.mark.parametrize("text", ["the timed test", "timed events", "a timed sprint"])
def test_a_bare_timed_in_a_topic_is_not_a_timed_poll(text):
    assert parse_kind(text).ask_when is False
    assert parse_kind(text, loose=True).ask_when is True          # …but is in the ask itself


@pytest.mark.parametrize("text", [
    "make a timed poll", "with a timer", "a time-limited poll", "auto-close", "timed poll",
])
def test_these_say_it_is_timed_with_no_length(text):
    assert parse_kind(text).ask_when is True


@pytest.mark.parametrize("text", [
    "bowling, quiz, karaoke", "pizza and trivia", "lunch and public", "bowling, public, karaoke",
    "who voted for him", "who voted", "poll about who votes", "see you at the public pool",
])
def test_a_list_of_topics_or_a_phrase_in_one_is_no_kind(text):
    got = parse_kind(text)
    assert got.quiz is False and got.anonymous is True


@pytest.mark.parametrize("text, expect", [
    ("tacos, quiz", dict(quiz=True)), ("tacos, public", dict(anonymous=False)),
    ("lunch, public, closes in 2 hours", dict(anonymous=False, open_seconds=7200)),
    ("tacos, quiz, public", dict(quiz=True, anonymous=False)),
    ("lunch, quiz.", dict(quiz=True)), ("lunch, public and timed", dict(anonymous=False)),
    ("tacos, quiz and public", dict(quiz=True)),            # "and public" has no comma before it
])
def test_a_kind_at_the_end_of_a_list_or_before_another_kind_still_is_one(text, expect):
    got = parse_kind(text)
    for name, value in expect.items():
        assert getattr(got, name) == value


@pytest.mark.parametrize("text", [
    "not public", "don't make it public", "regular, not public", "make it anonymous, nobody sees who voted",
    "no quiz", "not multiple", "without public votes", "isn't a quiz", "never public",
    "keep it anonymous", "anonymous, public transport",
])
def test_a_kind_word_that_is_negated_is_not_the_kind(text):
    got = parse_kind(text, loose=True)
    assert got.anonymous is True and got.quiz is False and got.multiple is not True


def test_a_no_with_a_comma_is_not_a_negation_of_what_follows():
    assert parse_kind("no, make it public").anonymous is False
    assert parse_kind("no. make it public").anonymous is False


@pytest.mark.parametrize("text", [
    "choose any day that works", "check any box", "pick every Friday", "only one of us can go",
    "one answer per person is fine", "there is only one answer",
])
def test_prose_with_pick_or_only_one_in_it_is_no_kind(text):
    got = parse_kind(text)
    assert got.multiple is None


@pytest.mark.parametrize("text, value", [
    ("pick all that apply", True), ("choose several", True), ("select all", True),
    ("pick only one", False), ("single answer", False), ("pick one", False),
])
def test_the_phrasings_that_do_say_how_many_still_do(text, value):
    assert parse_kind(text).multiple is value


# ── asking: "ask me what kind", and "timed" with no length ──────────────────

def test_ask_me_is_taken_out_of_what_the_poll_is_about():
    assert strip_ask_kind("ask me what kind") == ""
    assert strip_ask_kind("lunch, ask me what kind of poll") == "lunch"
    assert strip_ask_kind("what kind of pizza do we want") == "what kind of pizza do we want"
    assert strip_ask_kind("let me choose the kind. dinner") == "dinner"


@pytest.mark.asyncio
async def test_ask_me_what_kind_with_no_topic_asks_what_it_is_about_first():
    rt = _rt(reply=GOOD)
    msg = _msg("make a poll, ask me what kind")
    found = detect_poll_ask(msg.text)
    assert await make_poll(rt, msg, found.rest, ask_text=found.ask) == ASK_TOPIC
    rt.openai.cheap_completion.assert_not_awaited()
    ans = _answer("lunch spots, quiz")
    pending = take_pending(ans)
    await continue_poll(rt, ans, pending, "lunch spots, quiz")
    prompt = rt.openai.cheap_completion.await_args.args[0]
    assert "ask me what kind" not in prompt and "lunch spots" in prompt


@pytest.mark.asyncio
async def test_ask_me_what_kind_with_material_asks_the_kind_and_never_drafts_from_those_words():
    rt = _quizzer(reply=QUIZ)
    msg = _msg("poll this, ask me what kind", reply_to=_msg("capitals of europe"))
    found = detect_poll_ask(msg.text)
    assert await make_poll(rt, msg, found.rest, ask_text=found.ask) == ASK_KIND
    ans = _answer("a quiz")
    await continue_poll(rt, ans, take_pending(ans), "a quiz")
    prompt = rt.openai.cheap_completion.await_args.args[0]
    assert "ask me what kind" not in prompt and "capitals of europe" in prompt
    assert rt.bot.methods and rt.bot.methods[0].type == "quiz"


@pytest.mark.asyncio
async def test_a_timed_poll_with_no_topic_asks_both_in_one_question():
    rt = _rt(reply=GOOD)
    msg = _msg("make a timed poll")
    found = detect_poll_ask(msg.text)
    assert await make_poll(rt, msg, found.rest, ask_text=found.ask) == ASK_TOPIC_WHEN
    ans = _answer("lunch spots, 2 hours")
    pending = take_pending(ans)
    assert pending.want_when
    await continue_poll(rt, ans, pending, "lunch spots, 2 hours")
    assert rt.bot.send_poll.await_args.kwargs["open_period"] == 7200


@pytest.mark.asyncio
async def test_the_length_he_asked_for_is_the_length_not_a_number_in_the_topic():
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("x"), "", ask_text="timed")
    ans = _answer("beer 5 euros or 10 minutes of silence? 1 hour")
    await continue_poll(rt, ans, take_pending(ans), "beer 5 euros or 10 minutes of silence? 1 hour")
    # the first length written in the answer is the one read: say it last, or say it plainly
    assert rt.bot.send_poll.await_args.kwargs["open_period"] in (600, 3600)


@pytest.mark.asyncio
async def test_an_answer_with_no_length_gives_an_open_poll_and_says_so():
    rt = _rt(reply=GOOD)
    msg = _msg("x")
    await make_poll(rt, msg, "", ask_text="timed")
    ans = _answer("lunch spots")
    await continue_poll(rt, ans, take_pending(ans), "lunch spots")
    assert rt.bot.send_poll.await_args.kwargs["open_period"] is None
    assert NO_LENGTH == ans.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_a_typed_timed_poll_with_no_length_asks_then_says_it_stays_open():
    rt = _rt()
    msg = _msg("x")
    assert await make_poll(rt, msg, "where | pizza | tacos", ask_text="timed") == ASK_WHEN
    ans = _answer("no")
    await continue_poll(rt, ans, take_pending(ans), "no")
    assert rt.bot.send_poll.await_args.kwargs["open_period"] is None
    assert ans.reply.await_args.args[0] == NO_LENGTH


@pytest.mark.asyncio
async def test_a_timed_poll_with_a_length_says_nothing_extra():
    rt = _rt(reply=GOOD)
    msg = _msg("x")
    await make_poll(rt, msg, "lunch", ask_text="timed", answer="closes in 10 minutes", may_ask=False)
    msg.reply.assert_not_awaited()


# ── cancel words ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("word", [
    "cancel that", "nvm dale", "never mind the poll", "forget it please", "stop it",
    "drop it", "cancel", "Never mind!", "skip it dale",
])
async def test_the_ways_of_saying_never_mind_all_drop_it(word):
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("make a poll"), "")
    ans = _answer(word)
    assert await continue_poll(rt, ans, take_pending(ans), word) == DROPPED
    rt.openai.cheap_completion.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["no", "nope", "nah", "no poll", "no thanks", "nothing"])
async def test_a_bare_no_drops_a_question_about_what_it_is_about(word):
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("make a poll"), "")
    ans = _answer(word)
    assert await continue_poll(rt, ans, take_pending(ans), word) == DROPPED


@pytest.mark.asyncio
async def test_a_no_to_what_kind_means_no_particular_kind():
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("poll"), "lunch", ask_text="make a poll, ask me what kind")
    ans = _answer("no")
    out = await continue_poll(rt, ans, take_pending(ans), "no")
    assert out.startswith("[made a poll]")


@pytest.mark.asyncio
async def test_a_no_to_how_long_means_no_timer_and_he_says_it_stays_open():
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("x"), "lunch", ask_text="timed")
    ans = _answer("nope")
    out = await continue_poll(rt, ans, take_pending(ans), "nope")
    assert out.startswith("[made a poll]") and ans.reply.await_args.args[0] == NO_LENGTH


@pytest.mark.asyncio
async def test_a_long_message_that_starts_with_a_cancel_word_is_an_answer():
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("make a poll"), "")
    text = "no more arguing about pizza, tacos or sushi for friday dinner this week please"
    ans = _answer(text)
    out = await continue_poll(rt, ans, take_pending(ans), text)
    assert out.startswith("[made a poll]")


# ── the bare word "quiz" in an answer ───────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("answer, quiz", [
    ("quiz about capitals", True), ("a quiz on rivers", True), ("trivia, football", True),
    ("the pub quiz on friday", False), ("pub quiz night", False), ("quiz night", False),
    ("our quiz team", False), ("bowling or the quiz", False), ("this quiz show", False),
])
async def test_which_answers_mean_a_quiz(answer, quiz):
    rt = _quizzer(reply=QUIZ)
    await make_poll(rt, _msg("make a poll"), "")
    ans = _answer(answer)
    await continue_poll(rt, ans, take_pending(ans), answer)
    assert bool(rt.bot.methods) is quiz


@pytest.mark.asyncio
async def test_a_poll_typed_whole_as_the_answer_is_not_made_a_quiz_by_its_question():
    rt = _quizzer()
    await make_poll(rt, _msg("make a poll"), "")
    text = "Quiz or no quiz? | yes | no"
    ans = _answer(text)
    await continue_poll(rt, ans, take_pending(ans), text)
    assert not rt.bot.methods
    assert rt.bot.send_poll.await_args.kwargs["question"] == "Quiz or no quiz?"


# ── typed quiz marks ────────────────────────────────────────────────────────

@pytest.mark.parametrize("option", ["✔️ Paris", "☑️ Paris", "Paris ✔️", "✅️ Paris", "Paris ✓️"])
def test_a_tick_with_its_emoji_selector_marks_the_answer(option):
    marked, text = poll_maker._correct_mark(option)
    assert marked and text == "Paris"


def test_a_quiz_with_a_selector_tick_has_its_answer():
    d = piped("Capital? | London | ✔️ Paris | Rome", quiz=True)
    assert d.correct == (1,) and d.options == ["London", "Paris", "Rome"]


# ── the GIFs ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_clip_that_also_carries_a_keyword_tag_is_never_sent_unprompted(_gif_clock):
    db = _DB()
    await dg.add(db, ["conspiracy", "guns"], file_id="GUN", file_unique_id="U1")
    await dg.add(db, ["conspiracy"], file_id="OK", file_unique_id="U2")
    for _ in range(8):
        gr.forget_state()
        dg.forget_state()
        _gif_clock.advance(gr.QUIET_AFTER_BOT + 1)          # he has been quiet a while
        rt = SimpleNamespace(db=db, openai=SimpleNamespace(
            cheap_completion=AsyncMock(return_value="conspiracy")))
        msg = _chat_msg()
        assert await gr.ambient(rt, msg, "told you the printer was watching us") is True
        assert msg.reply_animation.await_args.args[0] == "OK"


@pytest.mark.asyncio
async def test_a_tag_only_such_clips_have_is_not_offered_at_all():
    db = _DB()
    await dg.add(db, ["panic", "pocketsand"], file_id="A", file_unique_id="U1")
    await dg.add(db, ["agree"], file_id="B", file_unique_id="U2")
    assert await gr.available_tags(db) == frozenset({"agree"})


@pytest.mark.asyncio
async def test_a_keyword_bit_can_still_send_a_clip_with_its_own_tag():
    db = _DB()
    await dg.add(db, ["conspiracy", "guns"], file_id="GUN", file_unique_id="U1")
    got = await dg.random_for_tag(db, "guns")
    assert got.file_id == "GUN"
    assert await dg.random_for_tag(db, "conspiracy", exclude_tags=gr.KEYWORD_TAGS) is None


@pytest.mark.asyncio
async def test_a_dead_clip_does_not_keep_winning_its_tag(monkeypatch, _gif_clock):
    db = _DB()
    await dg.add(db, ["agree"], url="https://media.tenor.com/dead.gif")
    await dg.add(db, ["agree"], file_id="LIVE", file_unique_id="U2")
    monkeypatch.setattr(dg, "_download", AsyncMock(return_value=None))
    sent = []
    for _ in range(6):
        msg = _chat_msg()
        _gif_clock.advance(dg.TRIGGER_TAG_GAP + dg.TRIGGER_CHAT_GAP)
        await dg.send_random(db, msg, "agree", fallback="x", pace=True)
        sent.append(msg.reply_animation.await_count)
    assert sum(sent) >= 5                                   # the live one carries on
    assert 1 in dg._failing


@pytest.mark.asyncio
async def test_a_dead_clip_is_tried_again_after_it_has_rested(monkeypatch, _gif_clock):
    db = _DB()
    await dg.add(db, ["agree"], url="https://media.tenor.com/dead.gif")
    download = AsyncMock(return_value=None)
    monkeypatch.setattr(dg, "_download", download)
    await dg.send_random(db, _chat_msg(), "agree", fallback="x")
    await dg.send_random(db, _chat_msg(), "agree", fallback="x")      # the only clip: tried anyway
    assert download.await_count == 2
    _gif_clock.advance(dg.FAIL_REST + 1)
    assert dg._failing[1] <= _gif_clock()


@pytest.mark.asyncio
async def test_a_clip_that_sends_is_not_left_marked_dead(monkeypatch, _gif_clock):
    db = _DB()
    await dg.add(db, ["agree"], file_id="A", file_unique_id="U1")
    dg._failing[1] = _gif_clock() + 100
    await dg.send_random(db, _chat_msg(), "agree")
    assert 1 not in dg._failing


def test_the_failing_clips_list_is_bounded(_gif_clock):
    for i in range(dg._STATE_LIMIT + 20):
        dg._note_failed(i)
    _gif_clock.advance(dg.FAIL_REST + 5)
    dg._note_failed(10_000)
    assert len(dg._failing) < 10


@pytest.mark.asyncio
async def test_a_forwarded_message_is_somebody_elses_words_not_a_moment_for_one():
    db = await _stocked("agree")
    rt = SimpleNamespace(db=db, openai=SimpleNamespace(cheap_completion=AsyncMock(return_value="agree")))
    for attr in ("forward_origin", "forward_date"):
        msg = _chat_msg()
        setattr(msg, attr, object())
        assert await gr.ambient(rt, msg, "yep exactly right") is False
    rt.openai.cheap_completion.assert_not_awaited()


@pytest.mark.parametrize("text", [
    "check example.com out please", "look at foo.gg for that", "bar.io is down", "see site.dev",
    "we lost my dog yesterday", "she had a heart attack", "he is in an ambulance",
    "i want to die honestly", "somebody was murdered nearby", "lost my job on friday",
])
def test_bare_domains_and_more_grave_things_are_not_a_moment(text):
    assert not gr.moment_ok(text)


@pytest.mark.parametrize("text", ["node.js is fine honestly", "e.g. this printer", "yep exactly right", "version 2.5 shipped"])
def test_a_dot_in_a_word_is_not_a_link(text):
    assert gr.moment_ok(text)


def test_the_docs_match_the_gif_and_poll_code():
    from pathlib import Path
    docs = " ".join((Path(__file__).resolve().parent.parent / "docs" / "COMMANDS.md").read_text().split())
    for needle in ("bare domains too", "not forwarded", "when the tag has more than one",
                   "half an hour", "`pocketsand`, `shsha` or `guns`", "poll ends in 2 hours",
                   "he says so", "one question asks both"):
        assert needle in docs, needle
    assert "ask what's missing" not in " ".join(
        (Path(__file__).resolve().parent.parent / "ipedro" / "capabilities.py").read_text().split())


# ── what the mutation sweep found the tests weren't pinning ─────────────────

def test_the_limits_are_the_documented_numbers_not_just_whatever_the_code_says():
    assert poll_maker._PENDING_TTL == 180.0                 # "three minutes"
    assert poll_maker._PENDING_MAX == 200
    assert poll_maker._PENDING_WITH_IMAGE_MAX == 8
    assert poll_maker.MAX_OPEN_SECONDS == 2_628_000 and poll_maker.MIN_OPEN_SECONDS == 5
    assert poll_maker.MAX_EXPLANATION == 200


@pytest.mark.asyncio
async def test_a_question_is_stale_after_three_minutes_by_the_clock(monkeypatch):
    clock = {"t": 5000.0}
    monkeypatch.setattr(poll_maker, "_now", lambda: clock["t"])
    rt = _rt(reply=GOOD)
    await make_poll(rt, _msg("make a poll"), "")
    clock["t"] += 179
    assert take_pending(_answer("pizza")) is not None
    await make_poll(rt, _msg("make a poll"), "")
    clock["t"] += 181
    assert take_pending(_answer("pizza")) is None


def test_a_re_asked_question_counts_as_the_newest_when_the_list_is_trimmed():
    poll_maker._remember((9, 0), poll_maker.Pending("topic", "", "", None, 1, 0.0))
    for i in range(1, poll_maker._PENDING_MAX):
        poll_maker._remember((9, i), poll_maker.Pending("topic", "", "", None, i, 0.0))
    poll_maker._remember((9, 0), poll_maker.Pending("topic", "again", "", None, 7, 0.0))   # asked again
    poll_maker._remember((9, 10_000), poll_maker.Pending("topic", "", "", None, 2, 0.0))
    assert (9, 0) in poll_maker._PENDING and (9, 1) not in poll_maker._PENDING
    assert poll_maker._PENDING[(9, 0)].request == "again"


def test_pictures_held_for_answers_are_limited_to_eight():
    big = Material = poll_maker.Material(image=b"\xff\xd8\xff", media_type="image/jpeg")
    for i in range(20):
        poll_maker._remember((8, i), poll_maker.Pending("kind", "x", "", big, i, 0.0))
    assert sum(1 for p in poll_maker._PENDING.values() if p.material) == 8
    assert len(poll_maker._PENDING) == 8


def test_a_lone_star_is_an_option_not_a_mark():
    assert poll_maker._correct_mark("*") == (False, "*")
    assert poll_maker._correct_mark("**") == (False, "**")
    assert poll_maker._correct_mark("Paris**") == (True, "Paris")
    assert poll_maker._correct_mark("  Paris   *  ") == (True, "Paris")


@pytest.mark.parametrize("text", [
    "abc2 hours", "v1.2.3 days", "mp3 minutes", "route66 hours", "x10 min", "1.5.2 hours", "a1 week",
])
def test_a_number_inside_a_word_or_a_version_is_not_a_length(text):
    assert parse_kind(text, loose=True).open_seconds is None


@pytest.mark.parametrize("text, expect", [
    ("which days work, all that apply", True), ("all that apply please", True),
    ("pick several", True), ("vote for as many as you like", True),
])
def test_several_answers_are_read_from_each_phrasing(text, expect):
    assert parse_kind(text).multiple is expect


def test_a_colon_far_into_the_message_is_not_a_prefix():
    text = "quiz, " * 25 + ": x"
    assert poll_maker.split_kind_prefix(text) == ("", text)


@pytest.mark.parametrize("text", [
    "that was fun, right?", "so we are doing this?", "well that went fine?", "the printer works now?",
])
def test_a_statement_with_a_question_mark_is_still_a_question(text):
    assert not gr.moment_ok(text)


@pytest.mark.parametrize("text, seconds", [
    ("2m", 120), ("5s", 5), ("1h", 3600), ("1w", 604800), ("1 min", 60), ("90 sec", 90),
    ("3 hrs", 10800), ("2 d", 172800), ("45 seconds", 45),
])
def test_every_unit_means_what_it_says(text, seconds):
    assert parse_kind(text, loose=True).open_seconds == seconds


@pytest.mark.asyncio
async def test_a_request_with_a_bar_in_it_that_is_not_a_whole_poll_is_still_read_for_its_kind():
    rt = _rt(reply=GOOD)
    msg = _msg("x")
    await make_poll(rt, msg, "about a|b testing, public")
    assert rt.bot.send_poll.await_args.kwargs["is_anonymous"] is False


@pytest.mark.asyncio
async def test_after_a_reaction_the_chat_rests_past_the_quiet_and_the_look_gap(_gif_clock):
    db = await _stocked("agree")
    rt = SimpleNamespace(db=db, openai=SimpleNamespace(cheap_completion=AsyncMock(return_value="agree")))
    assert await gr.ambient(rt, _chat_msg(), "yep exactly right") is True
    _gif_clock.advance(gr.QUIET_AFTER_BOT + gr.CHECK_GAP + 5)       # both of those are over
    rt.openai.cheap_completion.reset_mock()
    assert await gr.ambient(rt, _chat_msg(), "yep exactly right") is False
    rt.openai.cheap_completion.assert_not_awaited()                 # the 20 minutes aren't
    _gif_clock.advance(gr.AMBIENT_COOLDOWN)
    assert await gr.ambient(rt, _chat_msg(), "yep exactly right") is True
