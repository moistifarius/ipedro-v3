"""Gaps found by mutating ipedro/vision.py, the vision lines of handlers/chat.py,
capabilities.py and openai_client.describe_images (each test names the mutant
it kills). Every test passes on the real code.
"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import capabilities, vision
from ipedro.capabilities import capability_brief
from ipedro.handlers import chat
from ipedro.openai_client import OpenAIClient
from tests.test_captcha_intercept import _msg
from tests.test_openai_client import _claude_client, _FakeAnthropicMessages
from tests.test_video_vision import (  # noqa: F401  (_fresh is an autouse fixture)
    FRAMES, _fresh, _rt, _stub_context, _video, _vmsg, patched_extract,
)
from tests.test_vision import _blank_msg, _handler, _media_msg, _seeing_rt


def _settings(**kw):
    base = dict(video_vision_frames=6, video_vision_max_seconds=300, admin_ids=frozenset())
    base.update(kw)
    return SimpleNamespace(**base)


def _around(**kw):
    return _blank_msg(chat=SimpleNamespace(id=42), from_user=SimpleNamespace(id=7), **kw)


def _gif():
    return _around(animation=SimpleNamespace(
        file_id="g", file_unique_id="ug", duration=4, thumbnail=SimpleNamespace(file_id="gthumb")))


def _note():
    return _around(video_note=SimpleNamespace(
        file_id="n", file_unique_id="un", duration=12, thumbnail=SimpleNamespace(file_id="nthumb")))


def _file():
    return _around(document=SimpleNamespace(
        file_id="d", file_unique_id="ud", mime_type="video/mp4", file_name="clip.mp4",
        thumbnail=SimpleNamespace(file_id="dthumb")))


# ── which file is fetched, and what is cached ────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("make, fid", [(_vmsg, "v"), (_gif, "g"), (_note, "n"), (_file, "d")])
async def test_the_video_itself_is_downloaded_not_its_thumbnail(patched_extract, make, fid):
    """Mutant: `_download_video(rt.bot, media.source_file_id)` -> `media.file_id`.
    The thumbnail is a JPEG, container() refuses it, and every video quietly
    falls back to the thumbnail description: the feature never runs, and
    the mocked extract() can't tell."""
    rt = patched_extract(_rt())
    await vision.describe(rt, make(), transcribe=False)
    rt.bot.get_file.assert_awaited_once_with(fid)
    rt.extract.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_frames_only_description_serves_a_chat_without_voice_transcription(patched_extract):
    """Mutants: keys -> ("v2:",) always; keys without "v2n:" for transcribe=False.
    The chat that never turns sound into text would pay for the same look on
    every repost."""
    rt = patched_extract(_rt(cached={"v2n:uv": "frames only"}))
    assert await vision.describe(rt, _vmsg(), transcribe=False) == "[video: frames only]"
    rt.bot.get_file.assert_not_awaited()
    rt.extract.assert_not_awaited()
    rt.openai.describe_images.assert_not_awaited()
    assert not vision.ratelimit.LIMITER._hits


@pytest.mark.asyncio
async def test_nothing_is_cached_when_the_model_says_nothing(patched_extract, monkeypatch):
    """Mutant: `if description:` -> `if True:` (a failed look cached as NULL)."""
    rt = patched_extract(_rt(described=None))
    monkeypatch.setattr(vision, "_describe_media", AsyncMock(return_value=None))
    await vision.describe(rt, _vmsg(), transcribe=True)
    rt.db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_posts_with_no_sender_get_a_budget_per_chat_not_none_and_not_one_for_the_bot(
        patched_extract, monkeypatch):
    """Mutant: `user_id is not None and ...` exempts a message with no from_user
    (a channel post / an anonymous admin) from the per-person limit. And they
    are limited per chat, so one chat's anonymous admins can't use up every
    other chat's."""
    rt = patched_extract(_rt())
    monkeypatch.setattr(vision, "_describe_media", AsyncMock(return_value="a blurry thumbnail"))
    for i in range(12):
        msg = _vmsg(_video(file_unique_id=f"u{i}"), chat_id=500)         # the same chat
        msg.from_user = None
        await vision.describe(rt, msg, transcribe=False)
    assert rt.extract.await_count == 8
    other = _vmsg(_video(file_unique_id="elsewhere"), chat_id=501)
    other.from_user = None
    await vision.describe(rt, other, transcribe=False)
    assert rt.extract.await_count == 9                                   # another chat, its own budget


# ── limits that come from settings ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_frame_and_length_limits_are_the_deployments(patched_extract):
    """Mutants: max_frames=6 / max_seconds=10**9 hard-coded in the call."""
    rt = patched_extract(_rt(settings=_settings(video_vision_frames=3, video_vision_max_seconds=120)))
    await vision.describe(rt, _vmsg(_video(duration=120)), transcribe=True)
    assert rt.extract.await_args.kwargs == {"max_frames": 3, "want_audio": True, "max_seconds": 120}


@pytest.mark.asyncio
@pytest.mark.parametrize("size, watched", [(20_000_000, True), (20_000_001, False)])
async def test_twenty_megabytes_is_the_edge(patched_extract, monkeypatch, size, watched):
    """Mutant: `>` -> `>=` on the download ceiling."""
    rt = patched_extract(_rt())
    rt.bot.get_file.return_value = SimpleNamespace(file_path="p", file_size=size)
    monkeypatch.setattr(vision, "_describe_media", AsyncMock(return_value="a blurry thumbnail"))
    note = await vision.describe(rt, _vmsg(), transcribe=True)
    assert note == ("[video: a man runs from a duck]" if watched
                    else "[video, one frame only: a blurry thumbnail]")


@pytest.mark.asyncio
async def test_exactly_the_longest_allowed_video_is_still_watched(patched_extract):
    """Mutant: `>` -> `>=` on the claimed length (the real check is ffprobe's)."""
    rt = patched_extract(_rt())
    await vision.describe(rt, _vmsg(_video(duration=300)), transcribe=True)
    rt.extract.assert_awaited_once()


# ── what is passed on, and who is billed ─────────────────────────────────────

@pytest.mark.asyncio
async def test_the_whisper_and_vision_calls_are_billed_to_the_chat(patched_extract):
    """Mutants: chat_id dropped from transcribe() / describe_images(); the
    usage log is keyed on it."""
    rt = patched_extract(_rt())
    await vision.describe(rt, _vmsg(chat_id=99), transcribe=True)
    assert rt.openai.transcribe.await_args.kwargs["chat_id"] == 99
    assert rt.openai.describe_images.await_args.kwargs["chat_id"] == 99


@pytest.mark.asyncio
async def test_the_transcript_cap_is_2500_characters(patched_extract):
    """Mutant: _MAX_TRANSCRIPT_CHARS 2500 -> 4000 (the old test compares the
    clipped text with the constant itself)."""
    rt = patched_extract(_rt(transcript="\u00e9" * 9000))
    await vision.describe(rt, _vmsg(), transcribe=True)
    prompt = rt.openai.describe_images.await_args.kwargs["prompt"]
    assert prompt.count("\u00e9") == 2500


# ── typing indicator ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_typing_is_kept_up_while_a_slow_look_runs_and_stops_when_it_ends(patched_extract, monkeypatch):
    """Mutants: pulse never started; pulse task never cancelled (the old test
    waits 0.05 s of a 4 s pulse, so it can't see a leak); pulse interval 4 -> ~0."""
    real_sleep = asyncio.sleep
    requested = []

    async def quick(delay, *a, **k):
        requested.append(delay)
        return await real_sleep(0.01, *a, **k)

    rt = patched_extract(_rt())
    parts = rt.extract.return_value

    async def slow(*a, **k):
        await real_sleep(0.2)
        return parts

    rt.extract.side_effect = slow
    monkeypatch.setattr(vision.asyncio, "sleep", quick)
    await vision.look(rt, _vmsg(), transcribe=False, typing=True)
    assert set(requested) == {4}                           # the documented interval
    during = rt.bot.send_chat_action.await_count
    assert during >= 4
    await real_sleep(0.1)
    assert rt.bot.send_chat_action.await_count == during   # and it stopped with the look


@pytest.mark.asyncio
async def test_one_failed_typing_pulse_does_not_end_the_indicator(patched_extract, monkeypatch):
    """Mutant: the pulse's send_chat_action unguarded. The first failure kills
    the pulse task (silently), and "typing..." goes dead for the rest of the look."""
    real_sleep = asyncio.sleep

    async def quick(delay, *a, **k):
        return await real_sleep(0.01, *a, **k)

    rt = patched_extract(_rt())
    parts = rt.extract.return_value
    n = {"calls": 0}

    async def flaky(chat_id, action):
        n["calls"] += 1
        if n["calls"] == 2:
            raise RuntimeError("flood wait")

    rt.bot.send_chat_action.side_effect = flaky

    async def slow(*a, **k):
        await real_sleep(0.2)
        return parts

    rt.extract.side_effect = slow
    monkeypatch.setattr(vision.asyncio, "sleep", quick)
    await vision.look(rt, _vmsg(), transcribe=False, typing=True)
    assert n["calls"] >= 5


@pytest.mark.asyncio
async def test_a_telegram_hiccup_on_typing_does_not_cost_the_video(patched_extract):
    """Mutant: the first send_chat_action unguarded. The exception lands in
    _watch_media's catch-all and the video silently becomes a thumbnail."""
    rt = patched_extract(_rt())
    rt.bot.send_chat_action.side_effect = RuntimeError("flood wait")
    seen = await vision.look(rt, _vmsg(), transcribe=True, typing=True)
    assert seen.note == "[video: a man runs from a duck]"


# ── handlers/chat.py ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True, False])
async def test_the_message_and_the_one_it_replies_to_both_follow_the_chats_voice_setting(monkeypatch, voice):
    """Mutants on the reply path: transcribe=True / False / dropped. The old test
    asserted `{"transcribe": True} in kwargs`, which the message's own look()
    call already satisfies."""
    rt = _seeing_rt(monkeypatch)
    _stub_context(monkeypatch)
    rt.chats.get_config.return_value.voice_transcribe = voice
    replied = _media_msg(text=None)
    replied.from_user = SimpleNamespace(id=99, is_bot=False, username="o", first_name="O", last_name=None)
    msg = _msg(text="rusty what is this")
    msg.reply_to_message = replied
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    calls = vision.look.await_args_list
    assert [c.args[1] for c in calls] == [msg, replied]
    own, quoted = [c.kwargs for c in calls]
    assert own["transcribe"] is voice and "watch" not in own
    assert quoted == {"transcribe": voice, "watch": False, "typing": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("vision_on, ffmpeg, says_frames", [
    (True, True, True),
    (True, False, False),          # no ffmpeg / frames switched off: still the old line
    (False, True, False),          # the chat has looking switched off
])
async def test_the_brief_claims_frames_only_where_videos_really_are_watched(
        monkeypatch, vision_on, ffmpeg, says_frames):
    """Mutants: video=True / False / dropped / without the chat switch / without watching()."""
    rt = _seeing_rt(monkeypatch)
    rt.chats.get_config.return_value.vision_enabled = vision_on
    monkeypatch.setattr(vision, "watching", lambda _rt: ffmpeg)
    captured = {}

    async def fake_build(**kwargs):
        captured.update(kwargs)
        from ipedro.memory.context_builder import BuiltContext
        return BuiltContext(messages=[{"role": "user", "content": "x"}], tokens=1)

    monkeypatch.setattr(chat, "build_context", fake_build)
    msg = _msg(text="rusty hello")
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    brief = captured["capabilities"]
    assert ("handful of frames" in brief) is says_frames
    assert (capabilities._WATCH_LINE in brief) is (not says_frames)


def test_the_video_line_keeps_its_limits():
    """Mutant: the 'don't claim to have seen every moment, or to hear music or
    tone' clause removed; nothing else pins the honesty half of the line."""
    on = capability_brief(video=True)
    assert "don't claim to have seen every moment" in on and "music or tone" in on


# ── describe_images ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_describe_images_bills_the_chat_on_the_claude_path():
    """Mutants: chat_id=None / usage logging skipped / expensive model."""
    msgs = _FakeAnthropicMessages(text="ok")
    client = _claude_client(msgs)
    client._log_usage = AsyncMock()
    await client.describe_images([b"a", b"b", b"c"], prompt="p", chat_id=42)
    kw = client._log_usage.await_args.kwargs
    assert kw["kind"] == "vision" and kw["chat_id"] == 42 and kw["model"] == client.cheap_claude_model
    assert (kw["prompt_tokens"], kw["completion_tokens"]) == (10, 5)
    assert msgs.calls[0]["model"] == client.cheap_claude_model


@pytest.mark.asyncio
async def test_describe_images_openai_path_keeps_type_budget_order_and_billing():
    """Mutants: data: URL media type hard-coded; max_tokens ignored; chat_id
    dropped; only the first frame sent."""
    seen = {}

    class _Comp:
        async def create(self, **kw):
            seen.update(kw)
            return type("R", (), {
                "choices": [type("C", (), {"message": type("M", (), {"content": " ok "})()})()],
                "usage": type("U", (), {"prompt_tokens": 7, "completion_tokens": 3})(),
            })()

    client = _claude_client(_FakeAnthropicMessages(error=RuntimeError("anthropic down")))
    client._openai = type("O", (), {"chat": type("Ch", (), {"completions": _Comp()})()})()
    client._log_usage = AsyncMock()
    out = await client.describe_images(
        [b"one", b"two"], media_type="image/jpeg", prompt="p", max_tokens=400, chat_id=42)
    assert out == "ok"
    assert seen["max_tokens"] == 400 and seen["model"] == client.cheap_openai_model
    urls = [c["image_url"]["url"] for c in seen["messages"][0]["content"] if c["type"] == "image_url"]
    assert urls == ["data:image/jpeg;base64," + base64.b64encode(f).decode() for f in (b"one", b"two")]
    kw = client._log_usage.await_args.kwargs
    assert kw["kind"] == "vision" and kw["chat_id"] == 42
    assert (kw["prompt_tokens"], kw["completion_tokens"]) == (7, 3)
