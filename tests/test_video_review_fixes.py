"""What the video review found, pinned.

A 750 KB clip that decodes into gigabytes; slots spent on a video that was
never going to be watched; a reply to a video starting a paid watch charged to
whoever posted it; a failed transcription remembered as 'no speech'; Whisper
inventing 'Thanks for watching'; and a few smaller things around them.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import capabilities, hub, prompts, ratelimit, video_frames as vf, vision
from ipedro.capabilities import capability_brief
from ipedro.handlers import chat
from ipedro.openai_client import OpenAIClient, _AUDIO_PER_MINUTE
from tests.test_captcha_intercept import _msg
from tests.test_video_frames import clips, needs_ffmpeg  # noqa: F401  (fixture)
from tests.test_video_vision import (  # noqa: F401  (_fresh is an autouse fixture)
    FRAMES, MP4, _fresh, _rt, _stub_context, _video, _vmsg, patched_extract,
)
from tests.test_vision import _handler, _media_msg, _seeing_rt


# ── the extractor ────────────────────────────────────────────────────────────

def _canned(code, out=b"", err=b""):
    async def run(argv):
        return code, out, err
    return run


def _probe_json(*, w=1920, h=1080, duration="10.0", stream_duration=None, audio=True):
    video = {"codec_type": "video", "width": w, "height": h}
    if stream_duration is not None:
        video["duration"] = stream_duration
    streams = [video] + ([{"codec_type": "audio"}] if audio else [])
    return json.dumps({"format": {"duration": duration}, "streams": streams}).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("w, h, ok", [
    (3840, 2160, True), (4096, 2160, True), (4096, 2161, False),
    (7680, 4320, False), (16000, 16000, False), (65535, 65535, False),
])
async def test_the_real_picture_size_decides_not_the_size_the_file_claims(monkeypatch, w, h, ok):
    monkeypatch.setattr(vf, "_run", _canned(0, _probe_json(w=w, h=h)))
    assert (await vf._probe("p", "mov") is not None) is ok


def test_the_limit_is_4k():
    assert vf.MAX_PIXELS == 4096 * 2160


@pytest.mark.asyncio
async def test_a_probe_with_no_size_at_all_is_not_a_reason_to_refuse(monkeypatch):
    out = json.dumps({"format": {"duration": "5"}, "streams": [{"codec_type": "video"}]}).encode()
    monkeypatch.setattr(vf, "_run", _canned(0, out))
    assert await vf._probe("p", "mov") == (5.0, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream, expected", [
    ("2.0", 2.0),            # the picture ends long before the sound
    ("N/A", 10.0),           # webm/mkv: no per-stream duration, keep the file's
    (None, 10.0),
    ("30.0", 10.0),          # a longer claim than the file's is not believed
])
async def test_the_pictures_own_duration_wins_when_it_is_shorter(monkeypatch, stream, expected):
    monkeypatch.setattr(vf, "_run", _canned(0, _probe_json(duration="10.0", stream_duration=stream)))
    assert (await vf._probe("p", "mov"))[0] == expected


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_real_decoder_bomb_is_refused_before_it_is_decoded(tmp_path):
    """16000x16000 of black, 750 KB: it used to cost ~2.7 GB and half a minute."""
    path = tmp_path / "bomb.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "color=c=black:s=16000x16000:r=1:d=3", "-c:v", "libx264", "-preset",
         "ultrafast", "-crf", "40", "-pix_fmt", "yuv420p", str(path)], check=True)
    assert path.stat().st_size < 2_000_000
    calls = []
    real = vf._run

    async def spy(argv):
        calls.append(argv[0])
        return await real(argv)

    vf._run = spy
    try:
        assert await vf.extract(path.read_bytes(), max_frames=6, want_audio=True,
                                max_seconds=300) is None
    finally:
        vf._run = real
    assert calls == ["ffprobe"]                       # no frame was ever decoded


@needs_ffmpeg
@pytest.mark.asyncio
async def test_real_4k_is_still_watched(tmp_path):
    path = tmp_path / "uhd.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "testsrc=s=3840x2160:r=2:d=3", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", str(path)], check=True)
    parts = await vf.extract(path.read_bytes(), max_frames=4, want_audio=False, max_seconds=300)
    assert parts and len(parts.frames) == 4


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_one_picture_video_still_gives_that_picture(tmp_path):
    """Seeking lands on the frame AFTER the time asked for; a clip with a single
    still has none there, and used to give back nothing at all."""
    path = tmp_path / "still.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "color=c=red:s=128x128:r=1:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True)
    parts = await vf.extract(path.read_bytes(), max_frames=6, want_audio=False, max_seconds=300)
    assert parts is not None and len(parts.frames) == 1


@pytest.mark.asyncio
async def test_a_spot_that_gives_no_frame_is_retried_at_the_start_and_not_repeated(
        monkeypatch, tmp_path):
    asked = []

    async def frame(path, fmt, at):
        asked.append(at)
        return None if at > 0 else b"\xff\xd8\xffstart"

    async def probe(path, fmt):
        return 8.0, False

    monkeypatch.setattr(vf, "_frame", frame)
    monkeypatch.setattr(vf, "_probe", probe)
    parts = await vf._extract(str(tmp_path), b"x", "mov", 4, False, 300)
    assert parts.frames == [b"\xff\xd8\xffstart"]       # four misses, one picture, once
    assert asked.count(0.0) == 4 and len(asked) == 8


@needs_ffmpeg
@pytest.mark.asyncio
async def test_two_identical_neighbouring_frames_are_one(clips):
    parts = await vf.extract(clips["silent"], max_frames=6, want_audio=False, max_seconds=300)
    assert all(a != b for a, b in zip(parts.frames, parts.frames[1:]))


@pytest.mark.asyncio
async def test_a_video_that_cannot_get_a_turn_is_turned_away_not_queued(monkeypatch):
    """Two slots, both held by someone's slow file: the next one waits a few
    seconds and then gets the thumbnail, instead of queueing behind it for
    minutes with its 20 MB in memory."""
    monkeypatch.setattr(vf, "available", lambda: True)
    monkeypatch.setattr(vf, "_QUEUE_TIMEOUT", 0.2)
    gate = vf._gate()
    await gate.acquire()
    await gate.acquire()
    try:
        mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
        t = asyncio.get_event_loop().time()
        assert await vf.extract(mp4, max_frames=2, want_audio=False, max_seconds=60) is None
        assert asyncio.get_event_loop().time() - t < 2.0
    finally:
        gate.release()
        gate.release()


def test_the_queue_wait_is_short():
    assert vf._QUEUE_TIMEOUT <= 15 and vf._QUEUE_TIMEOUT < vf._TOTAL_TIMEOUT


@pytest.mark.asyncio
async def test_a_slot_is_given_back_when_the_look_ends_however_it_ends(monkeypatch):
    monkeypatch.setattr(vf, "available", lambda: True)

    async def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(vf, "_extract", boom)
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
    for _ in range(5):
        assert await vf.extract(mp4, max_frames=2, want_audio=False, max_seconds=60) is None
    assert vf._gate()._value == 2


# ── slots: spent only on a look that costs something ─────────────────────────

def _slots_used():
    return {k: len(v) for k, v in ratelimit.LIMITER._hits.items() if v}


@pytest.mark.asyncio
async def test_a_video_over_twenty_megabytes_costs_nobody_a_slot(patched_extract, monkeypatch):
    rt = patched_extract(_rt())
    monkeypatch.setattr(vision, "_describe_media", AsyncMock(return_value="thumb"))
    big = _video(file_size=20_000_001)
    note = await vision.describe(rt, _vmsg(big), transcribe=True)
    assert note == "[video, one frame only: thumb]"
    assert _slots_used() == {}
    rt.bot.get_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["download", "no_data", "not_a_video"])
async def test_a_look_that_never_got_going_gives_its_slots_back(patched_extract, monkeypatch, how):
    rt = patched_extract(_rt(parts=None))
    monkeypatch.setattr(vision, "_describe_media", AsyncMock(return_value=None))
    if how == "download":
        rt.bot.download_file.side_effect = RuntimeError("telegram")
    elif how == "no_data":
        rt.bot.get_file.return_value = SimpleNamespace(file_path="p", file_size=30_000_000)
    await vision.describe(rt, _vmsg(), transcribe=True)
    assert _slots_used() == {}


@pytest.mark.asyncio
async def test_a_look_that_ran_ffmpeg_keeps_its_slots_even_if_the_model_fails(patched_extract, monkeypatch):
    rt = patched_extract(_rt(described=None))
    monkeypatch.setattr(vision, "_describe_media", AsyncMock(return_value=None))
    await vision.describe(rt, _vmsg(), transcribe=True)
    assert _slots_used() == {("video", "user", 7): 1, ("video", "chat", 42): 1}


@pytest.mark.asyncio
async def test_bot_admins_use_no_slots(patched_extract):
    rt = patched_extract(_rt(settings=SimpleNamespace(
        video_vision_frames=6, video_vision_max_seconds=300, admin_ids=frozenset({7}))))
    await vision.describe(rt, _vmsg(), transcribe=True)
    assert _slots_used() == {}


@pytest.mark.asyncio
async def test_the_chat_limit_refusing_gives_the_person_theirs_back(patched_extract, monkeypatch):
    rt = patched_extract(_rt())
    monkeypatch.setattr(vision, "_describe_media", AsyncMock(return_value="thumb"))
    for i in range(20):                                    # fill the chat with other people
        await vision.describe(rt, _vmsg(_video(file_unique_id=f"f{i}"), user_id=100 + i), transcribe=False)
    await vision.describe(rt, _vmsg(_video(file_unique_id="late"), user_id=7), transcribe=False)
    assert ("video", "user", 7) not in _slots_used()


# ── the same file, once ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_video_posted_twice_at_once_is_watched_once(patched_extract):
    rt = patched_extract(_rt())
    parts = rt.extract.return_value
    started = asyncio.Event()

    async def slow(*a, **k):
        started.set()
        await asyncio.sleep(0.1)
        return parts

    rt.extract.side_effect = slow
    a, b = await asyncio.gather(
        vision.describe(rt, _vmsg(), transcribe=True),
        vision.describe(rt, _vmsg(user_id=8), transcribe=True),
    )
    assert a == b == "[video: a man runs from a duck]"
    assert rt.extract.await_count == 1 and rt.openai.describe_images.await_count == 1
    assert _slots_used() == {("video", "user", 7): 1, ("video", "chat", 42): 1}   # only the first paid
    assert vision._inflight == {}


@pytest.mark.asyncio
async def test_different_videos_are_not_merged(patched_extract):
    rt = patched_extract(_rt())
    await asyncio.gather(
        vision.describe(rt, _vmsg(_video(file_unique_id="a")), transcribe=True),
        vision.describe(rt, _vmsg(_video(file_unique_id="b")), transcribe=True),
    )
    assert rt.extract.await_count == 2


@pytest.mark.asyncio
async def test_a_watcher_that_gives_up_does_not_cancel_the_shared_look(patched_extract):
    rt = patched_extract(_rt())
    parts = rt.extract.return_value

    async def slow(*a, **k):
        await asyncio.sleep(0.15)
        return parts

    rt.extract.side_effect = slow
    first = asyncio.create_task(vision.describe(rt, _vmsg(), transcribe=True))
    await asyncio.sleep(0.02)
    second = asyncio.create_task(vision.describe(rt, _vmsg(user_id=8), transcribe=True))
    await asyncio.sleep(0.02)
    first.cancel()
    assert await second == "[video: a man runs from a duck]"


# ── a reply to a video does not start a paid watch ───────────────────────────

@pytest.mark.asyncio
async def test_asking_about_a_video_by_replying_uses_only_what_is_cached(patched_extract, monkeypatch):
    rt = patched_extract(_rt(cached={"v2:uv": "already watched"}))
    assert await vision.describe(rt, _vmsg(), transcribe=True, watch=False) == "[video: already watched]"
    rt.extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unwatched_video_replied_to_is_the_thumbnail_and_costs_no_one_a_slot(
        patched_extract, monkeypatch):
    rt = patched_extract(_rt())
    monkeypatch.setattr(vision, "_describe_media", AsyncMock(return_value="thumb"))
    note = await vision.describe(rt, _vmsg(), transcribe=True, watch=False)
    assert note == "[video, one frame only: thumb]"
    rt.bot.get_file.assert_not_awaited()
    rt.extract.assert_not_awaited()
    assert _slots_used() == {}


# ── the sound ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_transcription_that_failed_is_not_remembered_as_no_speech(patched_extract):
    rt = patched_extract(_rt(transcript=None))                # Whisper returned nothing
    note = await vision.describe(rt, _vmsg(), transcribe=True)
    assert note == "[video: a man runs from a duck]"
    key = rt.db.execute.await_args.args[1]
    assert key == "v2n:uv"                                    # frames only: a voice-on chat tries again


@pytest.mark.asyncio
async def test_a_transcription_that_worked_is_remembered_with_the_sound(patched_extract):
    rt = patched_extract(_rt(transcript="we have to go, now"))
    await vision.describe(rt, _vmsg(), transcribe=True)
    assert rt.db.execute.await_args.args[1] == "v2:uv"


@pytest.mark.asyncio
async def test_filler_a_transcriber_invents_is_not_passed_off_as_speech(patched_extract):
    rt = patched_extract(_rt(transcript="Thanks for watching!"))
    await vision.describe(rt, _vmsg(), transcribe=True)
    prompt = rt.openai.describe_images.await_args.kwargs["prompt"]
    assert "Thanks for watching" not in prompt and "don't mention sound" in prompt
    assert rt.db.execute.await_args.args[1] == "v2:uv"        # that IS the answer: no speech


@pytest.mark.parametrize("text", [
    "Thank you.", "Thanks for watching!", "Thank you for watching, and I'll see you in the next video.",
    "Please subscribe.", "Like and subscribe to my channel", "[Music]", "♪♪♪", "(applause)",
    "Subtitles by the Amara.org community", "Bye.", "you", "", None, "   ",
    "I'm looping. I'm looping. I'm looping. I'm looping. I'm looping.",
    "Translated by Mary Smith",
])
def test_whisper_filler_is_not_speech(text):
    assert vision.usable_transcript(text) is None


@pytest.mark.parametrize("text", [
    "Thank you, Dave, you're the best", "bruh", "I said thank you for watching my cat",
    "we have to go, now", "Wait, what? No way. That's insane. Do it again.",
    "Please subscribe me to the newsletter", "Goodbye, Mr. Gribble, and good luck",
    "The music was loud so I left.",
])
def test_real_speech_is_kept_whole(text):
    assert vision.usable_transcript(text) == text.strip()


def test_the_speech_prompt_tells_him_to_leave_out_what_doesnt_fit():
    assert "leave it out" in prompts.VIDEO_SPEECH_PRESENT


@pytest.mark.asyncio
async def test_whisper_is_billed_for_the_length_of_the_audio(monkeypatch):
    client = OpenAIClient(api_key="x")
    seen = []

    async def fake_create(**kw):
        return SimpleNamespace(text="hi")

    client._openai = SimpleNamespace(audio=SimpleNamespace(
        transcriptions=SimpleNamespace(create=fake_create)))
    client._log_usage = AsyncMock(side_effect=lambda **kw: seen.append(kw))
    import io
    await client.transcribe(io.BytesIO(b"x"), "a.mp3", chat_id=1, seconds=300)
    await client.transcribe(io.BytesIO(b"x"), "a.ogg", chat_id=1)               # a voice note
    await client.transcribe(io.BytesIO(b"x"), "a.mp3", chat_id=1, seconds=0.2)
    five_minutes, voice_note, tiny = (k["cost_usd"] for k in seen)
    assert five_minutes == pytest.approx(_AUDIO_PER_MINUTE * 5)
    assert voice_note == pytest.approx(_AUDIO_PER_MINUTE)                       # as it always was
    assert tiny == pytest.approx(_AUDIO_PER_MINUTE / 60)                        # at least a second


@pytest.mark.asyncio
async def test_the_video_path_bills_the_audio_length(patched_extract):
    rt = patched_extract(_rt())
    await vision.describe(rt, _vmsg(), transcribe=True)
    assert rt.openai.transcribe.await_args.kwargs["seconds"] == pytest.approx(19.6)
    rt2 = patched_extract(_rt(parts=vision.video_frames.VideoParts(
        seconds=400.0, frames=FRAMES, audio=b"mp3", had_audio_track=True)))
    await vision.describe(rt2, _vmsg(_video(file_unique_id="long", duration=290)), transcribe=True)
    assert rt2.openai.transcribe.await_args.kwargs["seconds"] == vf.AUDIO_SECONDS


# ── what he is told about it ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_video_that_was_only_glimpsed_says_so_in_the_transcript(patched_extract, monkeypatch):
    rt = patched_extract(_rt(parts=None))
    monkeypatch.setattr(vision, "_describe_media", AsyncMock(return_value="a man by a pond"))
    seen = await vision.look(rt, _vmsg(), transcribe=True)
    assert seen.note == "[video, one frame only: a man by a pond]"
    assert seen.description == "a man by a pond"          # the stored description stays bare


@pytest.mark.asyncio
async def test_a_watched_video_and_a_photo_are_noted_as_before(patched_extract):
    rt = patched_extract(_rt())
    assert (await vision.look(rt, _vmsg(), transcribe=True)).note == "[video: a man runs from a duck]"
    assert vision.note(vision.Media(kind="photo", file_unique_id="p", label="a photo"), "x") == "[photo: x]"
    assert vision.note(vision.Media(kind="video", file_unique_id="v", label="a video"), None,
                       glimpse=True) == "[a video]"


def test_the_brief_explains_the_one_frame_note():
    assert "one frame only" in capabilities._WATCH_LINE_VIDEO


def test_the_vision_off_line_no_longer_claims_a_note_arrives():
    from ipedro.db.repositories import ChatConfig
    cfg = ChatConfig(
        chat_id=1, response_policy="mention", ambient_probability=0.0, persona="dude",
        persona_custom=None, duckhunt_enabled=False, voice_transcribe=False,
        memory_enabled=True, vision_enabled=False)
    text = capabilities._chat_lines(cfg)
    assert "don't reach you at all" in text and "bare note" not in text


@pytest.mark.parametrize("n, seconds, frames_text, seconds_text", [
    (1, 2.0, "1 frame", "2.0"), (4, 3.0, "4 frames", "3.0"),
    (6, 10.0, "6 frames", "10"), (8, 119.6, "8 frames", "120"),
])
@pytest.mark.asyncio
async def test_the_prompt_counts_frames_and_seconds_properly(patched_extract, n, seconds, frames_text, seconds_text):
    parts = vf.VideoParts(seconds=seconds, frames=FRAMES[:1] * n, audio=None, had_audio_track=False)
    rt = patched_extract(_rt(parts=parts))
    await vision.describe(rt, _vmsg(), transcribe=False)
    prompt = rt.openai.describe_images.await_args.kwargs["prompt"]
    assert f"These are {frames_text} taken evenly" in prompt
    assert f"across a {seconds_text}-second video" in prompt


# ── in the chat ──────────────────────────────────────────────────────────────

class _Ent:
    def __init__(self, offset, length):
        self.type, self.offset, self.length = "mention", offset, length


def test_a_mention_in_a_caption_is_a_mention():
    video = SimpleNamespace(text=None, caption="@dalebot what is he saying", entities=None,
                            caption_entities=[_Ent(0, 8)])
    assert chat._has_bot_mention(video, "dalebot")
    plain = SimpleNamespace(text=None, caption="look at this", entities=None, caption_entities=None)
    assert not chat._has_bot_mention(plain, "dalebot")
    text = SimpleNamespace(text="hey @DaleBot", caption=None, entities=None)
    assert chat._has_bot_mention(text, "dalebot")
    neither = SimpleNamespace(text=None, caption=None, entities=None)
    assert not chat._has_bot_mention(neither, "dalebot")


@pytest.mark.asyncio
@pytest.mark.parametrize("caption, private, expected", [
    ("rusty look", False, True),            # his name
    ("look at this", False, False),         # nobody asked him anything
    ("look at this", True, True),           # a private chat: it's for him
])
async def test_typing_is_shown_only_when_he_is_about_to_answer(monkeypatch, caption, private, expected):
    rt = _seeing_rt(monkeypatch)
    _stub_context(monkeypatch)
    msg = _media_msg(text=caption)
    msg.caption = caption
    msg.text = None
    if private:
        msg.chat.type = "private"
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    assert vision.look.await_args.kwargs["typing"] is expected


@pytest.mark.asyncio
async def test_a_reply_to_the_bots_own_message_counts_as_addressed(monkeypatch):
    rt = _seeing_rt(monkeypatch)
    _stub_context(monkeypatch)
    msg = _media_msg(text=None)
    msg.text = msg.caption = None
    msg.reply_to_message = SimpleNamespace(message_id=5, from_user=SimpleNamespace(id=1))
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    assert vision.look.await_args_list[0].kwargs["typing"] is True


# ── other bots hear the same brief ───────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("vision_on, ffmpeg, says_video", [(True, True, True), (True, False, False), (False, True, False)])
async def test_a_bot_to_bot_reply_gets_the_same_honest_video_line(monkeypatch, vision_on, ffmpeg, says_video):
    from tests.test_hub import _post, _hub, HANK
    from ipedro import bot_messages
    from ipedro.memory import context_builder

    cfg = SimpleNamespace(memory_enabled=True, response_policy="mention", persona="dude",
                          persona_custom=None, vision_enabled=vision_on)
    rt = SimpleNamespace(
        chats=SimpleNamespace(get_config=AsyncMock(return_value=cfg)),
        users=SimpleNamespace(upsert_user=AsyncMock()),
        memory=SimpleNamespace(record_message=AsyncMock()),
        openai=SimpleNamespace(chat=AsyncMock(return_value="ok")),
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5))),
        activity=SimpleNamespace(log=AsyncMock()), settings=SimpleNamespace(), db=SimpleNamespace())
    briefs = []

    async def fake_build(**kw):
        return context_builder.BuiltContext(messages=[{"role": "user", "content": "x"}], tokens=1)

    def brief(cfg_, **kw):
        briefs.append(kw)
        return "x"

    async def no_flag(*a, **k):
        return False

    monkeypatch.setattr(context_builder, "build_context", fake_build)
    monkeypatch.setattr("ipedro.capabilities.capability_brief", brief)
    monkeypatch.setattr("ipedro.user_flags.has_flag", no_flag)
    monkeypatch.setattr(bot_messages, "track", lambda *a, **k: None)
    monkeypatch.setattr(vision, "watching", lambda _rt: ffmpeg)
    hub._left_until.clear()
    await hub.handle_post(rt, HANK, _hub(), _post(depth=1))
    assert briefs == [{"dale_flavor": False, **({"video": True} if says_video else {})}]
