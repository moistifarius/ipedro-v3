"""The bot reverses a video: the ask, the limits, and what it sends back.

What has to hold: only an ask at the start of a message, pointing at a video,
counts ("reverse psychology" does not); the result goes back as the same kind
of thing (a video, a looping GIF, a round video) as a reply; what Telegram says
of the file settles most refusals before a download; the per-person limit
counts work and not refusals; a video sent to be reversed isn't also watched;
and no model is ever called.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ipedro import identity, ratelimit, reverser, video_reverse as vr, vision
from ipedro.handlers import chat
from ipedro.handlers.common import _LIMIT_NOUNS
from ipedro.handlers.utility import build_router
from ipedro.reverser import (
    BUSY, FAILED, NO_VIDEO, NOT_VIDEO, TOO_BIG, TOO_LARGE_OUTPUT, UNAVAILABLE,
    ReverseAsk, detect_reverse_request, find_video, make_reverse, too_long,
)
from tests.test_poll_maker import _msg
from tests.test_vision import _handler as _chat_handler, _seeing_rt

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
MADE = vr.Reversed(data=b"\x00\x00\x00\x18ftypmp42" + b"x" * 40, seconds=9.6, width=640, height=360, has_audio=True)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    ratelimit.LIMITER.reset()
    monkeypatch.setattr(reverser, "track", Mock())
    yield
    ratelimit.LIMITER.reset()


# ── the ask ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, mute", [
    ("dale reverse this", False), ("reverse", False), ("Reverse it!", False),
    ("can you reverse this video please", False), ("hey dale, could you reverse that gif", False),
    ("play this backwards", False), ("play that video backwards", False),
    ("make this go backwards", False), ("make it play in reverse", False),
    ("run it backward", False), ("do this backwards", False), ("show it in reverse", False),
    ("rewind that gif", False), ("rewind", False), ("@dale_bot reverse this clip", False),
    ("reverse this and mute it", True), ("reverse that clip without sound", True),
    ("reverse this with no audio", True), ("reverse it silently", True),
    ("reverse the video above", False),
])
def test_an_ask_that_opens_the_message_is_recognised(text, mute):
    assert detect_reverse_request(text) == ReverseAsk(mute=mute)


@pytest.mark.parametrize("text", [
    "reverse psychology works", "reverse the order of the list", "what does reverse mean",
    "did you reverse it", "i want to reverse the polarity", "reverse engineering is fun",
    "we should reverse this decision", "they reversed the ban", "play the song",
    "play this song", "rewind to last week", "i reversed my car into a pole",
    "reverse this video and also tell me a joke about cats", "the video went in reverse",
    "dale reverse " + "this " * 80, "", None, "   ",
])
def test_talk_about_reversing_is_not_an_ask(text):
    assert detect_reverse_request(text) is None


def test_another_bots_names_are_its_own():
    hank = identity._build("Hank", ("hank", "hank hill"), False)
    assert detect_reverse_request("hank hill reverse this", names=hank.names_pattern) is not None
    assert detect_reverse_request("dale reverse this", names=hank.names_pattern) is None


@pytest.mark.parametrize("text", [
    "and " * 5000, "reverse " * 3000, "rusty shackleford " * 30 + "x",
    "reverse this " + "this " * 5000, "@a " * 3000, "reverse " + "!" * 4000,
])
def test_a_hostile_message_cannot_make_the_matcher_slow(text):
    started = time.perf_counter()
    detect_reverse_request(text)
    assert time.perf_counter() - started < 0.1


# ── what there is to reverse ────────────────────────────────────────────────

def _video(**kw):
    base = dict(file_id="v", file_unique_id="uv", duration=10, file_size=3_000_000, thumbnail=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _has(**media):
    return _msg(None, **media)


def test_its_own_video_is_found():
    found = find_video(_has(video=_video()))
    assert found is not None and found[0].kind == "video" and found[0].source_file_id == "v"


def test_what_it_replies_to_is_found_when_it_has_none_of_its_own():
    quoted = _has(video=_video(file_id="q"))
    found = find_video(_msg("reverse this", reply_to=quoted))
    assert found[0].source_file_id == "q" and found[1] is quoted


def test_its_own_video_beats_the_one_it_replies_to():
    own = _has(video=_video(file_id="own"), reply_to=_has(video=_video(file_id="q")))
    assert find_video(own)[0].source_file_id == "own"


@pytest.mark.parametrize("media, kind", [
    (dict(animation=SimpleNamespace(file_id="a", file_unique_id="ua", duration=3, file_size=900_000, thumbnail=None)), "gif"),
    (dict(video_note=SimpleNamespace(file_id="n", file_unique_id="un", duration=7, file_size=1_000_000, thumbnail=None)), "video note"),
    (dict(document=SimpleNamespace(file_id="d", file_unique_id="ud", mime_type="video/mp4", file_name="clip.mp4", file_size=2_000_000, thumbnail=None)), "video"),
])
def test_a_gif_a_round_video_and_a_video_sent_as_a_file_are_all_videos(media, kind):
    assert find_video(_has(**media))[0].kind == kind


@pytest.mark.parametrize("media", [
    dict(photo=[SimpleNamespace(file_id="p", file_unique_id="u", file_size=10)]),
    dict(sticker=SimpleNamespace(file_id="s", file_unique_id="u", emoji="x", set_name=None, is_animated=False, is_video=False, thumbnail=None)),
    dict(document=SimpleNamespace(file_id="d", file_unique_id="u", mime_type="application/pdf", file_name="a.pdf", file_size=10, thumbnail=None)),
    {},
])
def test_a_picture_a_sticker_a_pdf_and_nothing_are_not(media):
    assert find_video(_has(**media)) is None


# ── the job ─────────────────────────────────────────────────────────────────

def _rt(*, admin_ids=frozenset(), data=MP4, file_size=3_000_000):
    async def get_file(file_id):
        return SimpleNamespace(file_path=f"p/{file_id}", file_size=file_size)

    async def download_file(path, destination):
        destination.write(data)

    return SimpleNamespace(
        bot=SimpleNamespace(
            get_file=AsyncMock(side_effect=get_file),
            download_file=AsyncMock(side_effect=download_file),
            send_chat_action=AsyncMock(),
            send_video=AsyncMock(return_value=SimpleNamespace(message_id=500)),
            send_animation=AsyncMock(return_value=SimpleNamespace(message_id=501)),
            send_video_note=AsyncMock(return_value=SimpleNamespace(message_id=502)),
        ),
        openai=SimpleNamespace(
            cheap_completion=AsyncMock(), chat=AsyncMock(), describe_images=AsyncMock(),
            describe_image=AsyncMock(), transcribe=AsyncMock(),
        ),
        settings=SimpleNamespace(admin_ids=admin_ids),
    )


def _said(msg):
    return [c.args[0] for c in msg.reply.await_args_list]


@pytest.fixture
def reversing(monkeypatch):
    mock = AsyncMock(return_value=MADE)
    monkeypatch.setattr(vr, "reverse", mock)
    return mock


@pytest.mark.asyncio
async def test_a_video_comes_back_reversed_as_a_reply_that_is_a_video(reversing):
    rt = _rt()
    msg = _msg("reverse this", reply_to=_has(video=_video()))
    out = await make_reverse(rt, msg)
    assert out == "[reversed a video]"
    reversing.assert_awaited_once_with(MP4, mute=False)
    rt.bot.send_video.assert_awaited_once()
    kw = rt.bot.send_video.await_args.kwargs
    assert kw["chat_id"] == 42 and kw["supports_streaming"] is True
    assert (kw["width"], kw["height"], kw["duration"]) == (640, 360, 10)
    assert kw["video"].filename == "reversed.mp4" and kw["video"].data == MADE.data
    assert kw["reply_parameters"].message_id == 100
    assert kw["reply_parameters"].allow_sending_without_reply is True
    assert kw["disable_notification"] is True and kw["message_thread_id"] is None
    rt.bot.send_animation.assert_not_awaited()
    reverser.track.assert_called_once_with(42, 500, "[reversed video]")


@pytest.mark.asyncio
async def test_a_gif_comes_back_as_a_looping_gif(reversing):
    rt = _rt()
    gif = SimpleNamespace(file_id="a", file_unique_id="ua", duration=3, file_size=900_000, thumbnail=None)
    await make_reverse(rt, _msg(None, caption="/reverse", animation=gif))
    rt.bot.send_animation.assert_awaited_once()
    assert rt.bot.send_animation.await_args.kwargs["animation"].filename == "reversed.mp4"
    rt.bot.send_video.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_round_video_comes_back_as_a_round_video(reversing):
    rt = _rt()
    reversing.return_value = vr.Reversed(MADE.data, 6.4, 400, 400, True)
    note = SimpleNamespace(file_id="n", file_unique_id="un", duration=7, file_size=1_000_000, thumbnail=None)
    await make_reverse(rt, _msg(None, video_note=note))
    kw = rt.bot.send_video_note.await_args.kwargs
    assert kw["length"] == 400 and kw["duration"] == 6 and "video_note" in kw


@pytest.mark.asyncio
async def test_it_goes_to_the_topic_the_request_came_from(reversing):
    rt = _rt()
    msg = _msg(None, video=_video())
    msg.message_thread_id, msg.is_topic_message = 9, True
    await make_reverse(rt, msg)
    assert rt.bot.send_video.await_args.kwargs["message_thread_id"] == 9


@pytest.mark.asyncio
async def test_mute_is_passed_on_and_remembered(reversing):
    rt = _rt()
    out = await make_reverse(rt, _msg(None, video=_video()), mute=True)
    assert reversing.await_args.kwargs["mute"] is True and out.endswith("with the sound left out")


@pytest.mark.asyncio
async def test_nothing_to_reverse_says_what_to_give(reversing):
    rt = _rt()
    msg = _msg("reverse this")
    assert await make_reverse(rt, msg) == NO_VIDEO
    assert _said(msg) == [NO_VIDEO]
    reversing.assert_not_awaited()
    rt.bot.get_file.assert_not_awaited()
    assert ratelimit.LIMITER._hits == {}


@pytest.mark.asyncio
async def test_what_telegram_says_of_the_file_settles_big_and_long_before_a_download(reversing):
    rt = _rt()
    big = _msg(None, video=_video(file_size=21_000_000))
    assert await make_reverse(rt, big) == TOO_BIG
    long = _msg(None, video=_video(duration=31))
    assert await make_reverse(rt, long) == too_long(31)
    assert "31 seconds" in _said(long)[0] and "30" in _said(long)[0]
    rt.bot.get_file.assert_not_awaited()
    reversing.assert_not_awaited()
    assert ratelimit.LIMITER._hits == {}


@pytest.mark.asyncio
async def test_a_file_telegram_will_not_hand_over_is_too_big(reversing):
    rt = _rt(file_size=25_000_000)
    msg = _msg(None, video=_video(file_size=None))
    assert await make_reverse(rt, msg) == TOO_BIG
    reversing.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("code, text", [
    ("not-video", NOT_VIDEO), ("too-big", TOO_BIG), ("busy", BUSY),
    ("unavailable", UNAVAILABLE), ("failed", FAILED), ("too-large-output", TOO_LARGE_OUTPUT),
    ("big-picture", reverser.BIG_PICTURE), ("surprise", FAILED),
])
async def test_each_refusal_is_said_in_plain_words(reversing, code, text):
    reversing.side_effect = vr.Refused(code)
    msg = _msg(None, video=_video())
    assert await make_reverse(_rt(), msg) == text
    assert _said(msg) == [text]


@pytest.mark.asyncio
async def test_a_clip_that_turns_out_longer_than_it_said_is_refused_with_its_length(reversing):
    reversing.side_effect = vr.Refused("too-long", "47")
    msg = _msg(None, video=_video(duration=10))
    assert await make_reverse(_rt(), msg) == too_long(47.0)


@pytest.mark.asyncio
async def test_an_unexpected_error_is_a_plain_failure_not_a_crash(reversing):
    reversing.side_effect = RuntimeError("boom")
    msg = _msg(None, video=_video())
    assert await make_reverse(_rt(), msg) == FAILED


@pytest.mark.asyncio
async def test_a_failed_send_says_telegrams_reason(reversing):
    rt = _rt()
    rt.bot.send_video.side_effect = RuntimeError("Bad Request: not enough rights to send videos")
    msg = _msg(None, video=_video())
    out = await make_reverse(rt, msg)
    assert out == "Telegram wouldn't take it: Bad Request: not enough rights to send videos"
    reverser.track.assert_not_called()


@pytest.mark.asyncio
async def test_a_download_that_fails_outright_is_said_and_costs_nothing(reversing, monkeypatch):
    monkeypatch.setattr(vision, "_download_video", AsyncMock(return_value=None))
    msg = _msg(None, video=_video())
    assert await make_reverse(_rt(), msg) == TOO_BIG
    assert ratelimit.LIMITER._hits.get(("reverse", "user", 7)) in (None, [], __import__("collections").deque())


@pytest.mark.asyncio
async def test_a_chat_action_shows_while_it_works(reversing):
    rt = _rt()
    await make_reverse(rt, _msg(None, video=_video()))
    assert rt.bot.send_chat_action.await_args_list[0].args == (42, "upload_video")


@pytest.mark.asyncio
async def test_no_model_is_ever_called(reversing):
    rt = _rt()
    await make_reverse(rt, _msg(None, video=_video()))
    for name in ("cheap_completion", "chat", "describe_images", "describe_image", "transcribe"):
        getattr(rt.openai, name).assert_not_awaited()


# ── the limit ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_six_an_hour_then_it_says_so_and_does_nothing_more(reversing):
    rt = _rt()
    uses = ratelimit.LIMITS["reverse"][0]
    assert uses == 6
    for _ in range(uses):
        assert await make_reverse(rt, _msg(None, video=_video())) == "[reversed a video]"
    msg = _msg(None, video=_video())
    assert await make_reverse(rt, msg) is None
    assert "reversals for the hour" in _said(msg)[0]
    assert reversing.await_count == uses


@pytest.mark.asyncio
async def test_refusals_that_did_nothing_do_not_use_up_the_hour(reversing):
    rt = _rt()
    for code in ("not-video", "busy", "unavailable", "big-picture", "too-big"):
        reversing.side_effect = vr.Refused(code)
        await make_reverse(rt, _msg(None, video=_video()))
    reversing.side_effect = None
    reversing.return_value = MADE
    for _ in range(ratelimit.LIMITS["reverse"][0]):
        assert await make_reverse(rt, _msg(None, video=_video())) == "[reversed a video]"


@pytest.mark.asyncio
async def test_a_failure_after_the_work_started_does_count(reversing):
    rt = _rt()
    for _ in range(ratelimit.LIMITS["reverse"][0]):
        reversing.side_effect = vr.Refused("failed")
        await make_reverse(rt, _msg(None, video=_video()))
    msg = _msg(None, video=_video())
    assert await make_reverse(rt, msg) is None


@pytest.mark.asyncio
async def test_a_chat_has_a_budget_too_and_other_chats_are_not_held_up(reversing):
    rt = _rt()
    chat_uses = ratelimit.CHAT_LIMITS["reverse"][0]
    for i in range(chat_uses):
        assert await make_reverse(rt, _msg(None, video=_video(), user_id=1000 + i)) == "[reversed a video]"
    msg = _msg(None, video=_video(), user_id=5000)
    assert await make_reverse(rt, msg) is None
    assert await make_reverse(rt, _msg(None, video=_video(), user_id=5000, chat_id=43)) == "[reversed a video]"


@pytest.mark.asyncio
async def test_bot_admins_are_not_limited(reversing):
    rt = _rt(admin_ids=frozenset({7}))
    for _ in range(ratelimit.LIMITS["reverse"][0] + 4):
        assert await make_reverse(rt, _msg(None, video=_video())) == "[reversed a video]"


def test_the_limits_and_their_wording_exist():
    assert ratelimit.LIMITS["reverse"] == (6, 3600.0)
    assert ratelimit.CHAT_LIMITS["reverse"] == (15, 3600.0)
    assert _LIMIT_NOUNS["reverse"] == "reversals"


# ── in the chat and as a command ────────────────────────────────────────────

def _chat_rt(monkeypatch, *, policy="mention"):
    rt = _seeing_rt(monkeypatch, policy=policy)
    inner = _rt()
    for name in ("get_file", "download_file", "send_chat_action", "send_video", "send_animation", "send_video_note"):
        setattr(rt.bot, name, getattr(inner.bot, name))
    rt.bot.me = AsyncMock(return_value=SimpleNamespace(id=1, username="botname"))
    rt.settings.admin_ids = frozenset()
    return rt


def _in_chat(text, *, reply_to=None, caption=None, chat_type="group", **media):
    msg = _msg(text, caption=caption, reply_to=reply_to, chat_type=chat_type, **media)
    msg.voice = None
    return msg


@pytest.mark.asyncio
async def test_a_video_sent_with_the_ask_is_reversed_and_not_also_watched(monkeypatch, reversing):
    rt = _chat_rt(monkeypatch)
    look = AsyncMock(side_effect=AssertionError("a video sent to be reversed must not be watched"))
    monkeypatch.setattr(vision, "look", look)
    msg = _in_chat(None, caption="dale reverse this", video=_video())
    await _chat_handler(rt)(msg)
    look.assert_not_awaited()
    reversing.assert_awaited_once()
    rt.bot.send_video.assert_awaited_once()
    rt.openai.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_replying_to_a_video_with_the_ask_is_reversed_and_remembered(monkeypatch, reversing):
    rt = _chat_rt(monkeypatch)
    quoted = _has(video=_video(file_id="q"))
    quoted.from_user = SimpleNamespace(id=9, is_bot=False)
    await _chat_handler(rt)(_in_chat("dale play this backwards", reply_to=quoted))
    rt.bot.send_video.assert_awaited_once()
    said = [c.kwargs["content"] for c in rt.memory.record_message.await_args_list
            if c.kwargs["role"] == "assistant"]
    assert said == ["[reversed a video]"]
    rt.openai.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_ask_nobody_aimed_at_him_does_nothing(monkeypatch, reversing):
    rt = _chat_rt(monkeypatch)
    monkeypatch.setattr(chat.addressed, "wants_reply", AsyncMock(return_value=False))
    await _chat_handler(rt)(_in_chat("reverse this", reply_to=_has(video=_video())))
    reversing.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "dale what does reverse psychology mean", "dale reverse the order of these names",
    "dale did you reverse it",
])
async def test_talk_about_reversing_gets_an_ordinary_reply(monkeypatch, reversing, text):
    from ipedro.memory.context_builder import BuiltContext

    async def fake_build(**kwargs):
        return BuiltContext(messages=[{"role": "user", "content": "x"}], tokens=1)

    monkeypatch.setattr(chat, "build_context", fake_build)
    rt = _chat_rt(monkeypatch)
    await _chat_handler(rt)(_in_chat(text))
    reversing.assert_not_awaited()
    rt.openai.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_refusal_in_the_chat_is_said_and_written_down(monkeypatch, reversing):
    rt = _chat_rt(monkeypatch)
    reversing.side_effect = vr.Refused("not-video")
    msg = _in_chat("dale reverse this", reply_to=_has(video=_video()))
    await _chat_handler(rt)(msg)
    assert msg.reply.await_args.args[0] == NOT_VIDEO
    said = [c.kwargs["content"] for c in rt.memory.record_message.await_args_list
            if c.kwargs["role"] == "assistant"]
    assert said == [NOT_VIDEO]


def _command(rt):
    return next(h.callback for h in build_router(rt).observers["message"].handlers
                if h.callback.__name__ == "reverse")


def _cmd_rt():
    rt = _rt()
    rt.chats = SimpleNamespace(upsert_chat=AsyncMock(), get_config=AsyncMock())
    rt.users = SimpleNamespace(upsert_user=AsyncMock())
    return rt


@pytest.mark.asyncio
async def test_the_command_on_a_reply_reverses_it(reversing):
    rt = _cmd_rt()
    await _command(rt)(_msg("/reverse", reply_to=_has(video=_video())))
    rt.bot.send_video.assert_awaited_once()
    assert reversing.await_args.kwargs["mute"] is False


@pytest.mark.asyncio
async def test_the_command_as_a_caption_reverses_the_video_it_came_with(reversing):
    rt = _cmd_rt()
    await _command(rt)(_msg(None, caption="/reverse", video=_video()))
    rt.bot.send_video.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("text, mute", [("/reverse mute", True), ("/reverse Muted", True),
                                        ("/reverse silent", True), ("/reverse", False),
                                        ("/reverse please", False)])
async def test_the_command_takes_mute(reversing, text, mute):
    await _command(_cmd_rt())(_msg(text, reply_to=_has(video=_video())))
    assert reversing.await_args.kwargs["mute"] is mute


@pytest.mark.asyncio
async def test_the_command_with_no_video_says_what_to_give(reversing):
    msg = _msg("/reverse")
    await _command(_cmd_rt())(msg)
    assert _said(msg) == [NO_VIDEO]


# ── what he is told he can do, and where it is written ──────────────────────

def test_the_brief_the_catalog_and_the_help_name_it():
    from ipedro import capabilities
    from ipedro.handlers import basics
    from ipedro.handlers.command_catalog import COMMANDS
    assert "Reverse a video or GIF when asked" in capabilities.capability_brief()
    entry = next(c for c in COMMANDS if c.slug == "reverse")
    assert entry.usage.startswith("/reverse") and "backwards" in entry.desc
    assert "/reverse [mute]" in basics.HELP_TEXT_PUBLIC


def test_the_docs_describe_it():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    docs = " ".join((root / "docs" / "COMMANDS.md").read_text().split())
    for needle in ('### `/reverse`, and "reverse this"', "up to 30 seconds and 20 MB",
                   "400 MB in all", "6 an hour each, 15 an hour a chat",
                   "none of the original's metadata", "isn't also *watched*"):
        assert needle in docs, needle
    assert "/reverse" in (root / "README.md").read_text()
    assert "/reverse" in (root / "docs" / "SECURITY.md").read_text()
