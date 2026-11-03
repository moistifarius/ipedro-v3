"""Gaps found by mutating ipedro/video_frames.py (each test names the mutant it kills).

Every test here passes on the real code and fails on a one-line mutation of it.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess

import pytest

from ipedro import video_frames as vf
from tests.test_video_frames import clips, needs_ffmpeg  # noqa: F401  (fixture)


def _probe_bytes(data: bytes, *entries: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", "-show_entries", ":".join(entries), "-i", "-"],
        input=data, capture_output=True, check=True).stdout
    return json.loads(out)


def _make_pipe(*lavfi: str, args=()) -> bytes:
    """A clip written to a pipe: containers can't be seeked back to fill in
    the duration, as in a live or browser recording."""
    cmd = ["ffmpeg", "-loglevel", "error"]
    for i in lavfi:
        cmd += ["-f", "lavfi", "-i", i]
    return subprocess.run([*cmd, *args, "pipe:1"], capture_output=True, check=True).stdout


def _spy_run(monkeypatch):
    calls: list[list[str]] = []
    real = vf._run

    async def spy(argv):
        calls.append(list(argv))
        return await real(argv)

    monkeypatch.setattr(vf, "_run", spy)
    return calls


# ── what is refused before ffmpeg ───────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    b"#EXTM3U\n#EXTINF:5,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n",
    b"ffconcat version 1.0\nfile /etc/passwd\n",
    b"x" * 2000,
])
async def test_extract_itself_refuses_a_non_container_before_starting_anything(monkeypatch, data):
    """Mutant: extract() ignores container() (`fmt = fmt or "mov"`). The old test
    makes _run raise AssertionError, but extract's catch-all `except Exception`
    swallows it and returns None, so it passes with the gate removed. Record
    the calls instead."""
    calls = []

    async def spy(argv):
        calls.append(argv)
        return 1, b"", b""

    monkeypatch.setattr(vf, "available", lambda: True)
    monkeypatch.setattr(vf, "_run", spy)
    assert await vf.extract(data, max_frames=6, want_audio=True, max_seconds=300) is None
    assert calls == []


# ── the command lines ────────────────────────────────────────────────────────

@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.parametrize("clip, demuxer", [("talk", "mov"), ("webm", "matroska")])
async def test_every_process_gets_the_file_protocol_only_and_the_forced_demuxer(
        clips, monkeypatch, clip, demuxer):
    """Mutants: _COMMON / _probe drop -protocol_whitelist; _probe / _frame /
    _audio drop the forced -f. Nothing else observes either flag: ffmpeg's own
    probing also picks the right demuxer for an honest file."""
    calls = _spy_run(monkeypatch)
    parts = await vf.extract(clips[clip], max_frames=6, want_audio=True, max_seconds=300)
    assert parts is not None
    assert calls[0][0] == "ffprobe" and len(calls) >= 3
    for argv in calls:
        assert argv[0] in ("ffprobe", "ffmpeg")
        w = argv.index("-protocol_whitelist")
        assert argv[w + 1] == "file", argv
        f = argv.index("-f")                      # the FIRST -f is the input's
        assert argv[f + 1] == demuxer and f < argv.index("-i"), argv


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_silent_clip_never_starts_an_audio_process(clips, monkeypatch):
    """Mutant: `want_audio and has_audio` -> `want_audio`. The wasted run
    returned nothing, so the result looked the same."""
    calls = _spy_run(monkeypatch)
    parts = await vf.extract(clips["silent"], max_frames=6, want_audio=True, max_seconds=300)
    assert parts and parts.audio is None
    assert not any("-vn" in c for c in calls)                  # the audio extraction
    assert sum("-frames:v" in c for c in calls) == len(parts.frames)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_file_with_no_picture_costs_one_probe_and_nothing_else(clips, monkeypatch):
    """Mutant: _probe stops requiring a video stream. The frame runs all fail,
    so the old test (None either way) can't tell; but each is a process."""
    calls = _spy_run(monkeypatch)
    assert await vf.extract(clips["audio_only"], max_frames=6, want_audio=True, max_seconds=300) is None
    assert [c[0] for c in calls] == ["ffprobe"]


# ── what comes out ───────────────────────────────────────────────────────────

@needs_ffmpeg
@pytest.mark.asyncio
async def test_each_frame_is_one_picture(clips):
    """Mutant: drop `-frames:v 1`. ffmpeg then streams every frame from that
    point to the end, concatenated: one 'frame' that is dozens of JPEGs."""
    parts = await vf.extract(clips["talk"], max_frames=6, want_audio=False, max_seconds=300)
    assert [f.count(b"\xff\xd8\xff") for f in parts.frames] == [1] * 6


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_small_video_is_not_blown_up_and_the_width_is_the_documented_one(clips):
    """Mutants: scale=FRAME_WIDTH without min(); FRAME_WIDTH 768 -> 1024 (the
    portrait test compares against the constant itself)."""
    assert vf.FRAME_WIDTH == 768
    parts = await vf.extract(clips["talk"], max_frames=2, want_audio=False, max_seconds=300)
    info = _probe_bytes(parts.frames[0], "stream=width,height")["streams"][0]
    assert (info["width"], info["height"]) == (640, 360)           # as shot, not 768 wide


@pytest.fixture(scope="module")
def long_stereo(tmp_path_factory):
    """Nearly seven minutes of stereo sound under a tiny 1 fps picture."""
    if not vf.available():
        return b""
    path = tmp_path_factory.mktemp("long") / "long.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=64x64:r=1:d=400",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=400", "-ac", "2",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)],
        check=True)
    return path.read_bytes()


@needs_ffmpeg
@pytest.mark.asyncio
async def test_the_audio_is_five_minutes_of_small_mono_16k_mp3(long_stereo, tmp_path):
    """Mutants: no -t cap; AUDIO_SECONDS 300 -> 3000 (ten times the Whisper bill);
    drop -ac 1 (the real clips in this file are all mono); drop -ar 16000;
    -b:a 32k -> 320k."""
    parts = await vf.extract(long_stereo, max_frames=2, want_audio=True, max_seconds=600)
    mp3 = tmp_path / "a.mp3"
    mp3.write_bytes(parts.audio)
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", "-show_entries",
         "stream=codec_type,channels,sample_rate:format=duration", str(mp3)],
        capture_output=True, check=True).stdout)
    assert [s["codec_type"] for s in info["streams"]] == ["audio"]
    assert info["streams"][0]["channels"] == 1 and info["streams"][0]["sample_rate"] == "16000"
    assert float(info["format"]["duration"]) == pytest.approx(300.0, abs=2.0)    # of the 400 s there are
    assert len(parts.audio) < 300 * 32_000 / 8 * 1.3                             # ~32 kbit/s


def _canned(code, out=b"", err=b""):
    async def run(argv):
        return code, out, err
    return run


JPEG = b"\xff\xd8\xff" + b"\x00" * 64
GOOD_PROBE = json.dumps({"format": {"duration": "5.0"}, "streams": [{"codec_type": "video"}]}).encode()


@pytest.mark.asyncio
async def test_a_probe_that_exited_badly_is_a_failure_whatever_it_printed(monkeypatch):
    """Mutant: _probe stops checking the exit code."""
    monkeypatch.setattr(vf, "_run", _canned(0, GOOD_PROBE))
    assert await vf._probe("p", "mov") == (5.0, False)
    monkeypatch.setattr(vf, "_run", _canned(1, GOOD_PROBE))
    assert await vf._probe("p", "mov") is None


@pytest.mark.asyncio
async def test_a_frame_is_only_a_whole_small_jpeg_from_a_clean_exit(monkeypatch):
    """Mutants: drop the JPEG magic / exit code / size checks; _MAX_FRAME_BYTES 3.5M -> 35M."""
    monkeypatch.setattr(vf, "_run", _canned(0, JPEG))
    assert await vf._frame("p", "mov", 1.0) == JPEG
    monkeypatch.setattr(vf, "_run", _canned(1, JPEG))                   # died after a partial frame
    assert await vf._frame("p", "mov", 1.0) is None
    monkeypatch.setattr(vf, "_run", _canned(0, b"<html>not an image</html>"))
    assert await vf._frame("p", "mov", 1.0) is None
    assert vf._MAX_FRAME_BYTES <= 3_500_000                             # under every provider's ceiling
    monkeypatch.setattr(vf, "_run", _canned(0, JPEG + b"\x00" * vf._MAX_FRAME_BYTES))
    assert await vf._frame("p", "mov", 1.0) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code, err, kept", [
    (0, b"mean_volume: -20.0 dB", True),
    (0, b"mean_volume: -40.0 dB", True),            # quiet speech: kept (-45 dB line)
    (0, b"mean_volume: -50.0 dB", False),           # a fan / room tone: a transcriber invents words for it
    (0, b"mean_volume: -91.0 dB", False),
    (0, b"mean_volume: -inf dB", False),
    (0, b"", False),                                # no level report: don't hand unknown audio to Whisper
    (1, b"mean_volume: -20.0 dB", False),           # died partway: truncated mp3
])
async def test_audio_is_kept_only_when_it_is_audible_and_complete(monkeypatch, code, err, kept):
    """Mutants: SILENCE_DB -45 -> -35 / -60; no-report accepted; exit code ignored."""
    monkeypatch.setattr(vf, "_run", _canned(code, b"ID3mp3bytes", err))
    assert (await vf._audio("p", "mov") is not None) is kept


@pytest.mark.parametrize("seconds, wanted, n", [(30, 8, 6), (30.5, 8, 8), (8, 8, 4), (8.5, 8, 6)])
def test_the_frame_tiers_for_a_deployment_that_asks_for_eight(seconds, wanted, n):
    """Mutant: `seconds <= 30` -> `< 30` (invisible at the default of 6 frames)."""
    assert vf.frame_count(seconds, wanted) == n


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_video_that_does_not_say_how_long_it_is_is_refused():
    """Mutant: drop `seconds > 0`. A pipe-written WebM has no duration, so the
    length cap can't be applied and every frame would be taken at 0.00 s."""
    live = _make_pipe("testsrc=duration=4:size=320x240:rate=24",
                      args=["-c:v", "libvpx", "-b:v", "200k", "-f", "webm"])
    assert await vf.extract(live, max_frames=6, want_audio=False, max_seconds=300) is None


@needs_ffmpeg
@pytest.mark.asyncio
async def test_no_frames_wanted_runs_nothing_even_for_a_real_video(clips, monkeypatch):
    """Mutant: drop `max_frames < 1` (the old test's junk video dies in the
    probe anyway)."""
    calls = _spy_run(monkeypatch)
    assert await vf.extract(clips["talk"], max_frames=0, want_audio=True, max_seconds=300) is None
    assert calls == []


@needs_ffmpeg
@pytest.mark.asyncio
async def test_without_ffmpeg_nothing_is_started(clips, monkeypatch):
    """Mutant: drop `not available()`."""
    monkeypatch.setattr(vf, "available", lambda: False)
    calls = _spy_run(monkeypatch)
    assert await vf.extract(clips["talk"], max_frames=6, want_audio=True, max_seconds=300) is None
    assert calls == []


@needs_ffmpeg
@pytest.mark.asyncio
async def test_frames_that_fail_are_left_out_not_kept_as_none(clips, monkeypatch):
    """Mutants: append the None; drop `if not frames`."""
    real = vf._frame
    n = {"i": 0}

    async def flaky(path, fmt, at):
        n["i"] += 1
        return None if n["i"] == 2 else await real(path, fmt, at)

    monkeypatch.setattr(vf, "_frame", flaky)
    parts = await vf.extract(clips["talk"], max_frames=6, want_audio=False, max_seconds=300)
    # the failed spot is tried again at the start of the clip, so it isn't lost
    assert len(parts.frames) == 6 and None not in parts.frames

    async def none(path, fmt, at):
        return None

    monkeypatch.setattr(vf, "_frame", none)
    assert await vf.extract(clips["talk"], max_frames=6, want_audio=False, max_seconds=300) is None


# ── it always cleans up and always answers ───────────────────────────────────

def _spy_workdirs(monkeypatch, tmp_path):
    made = []
    real = vf.tempfile.mkdtemp

    def spy(*a, **k):
        d = real(*a, dir=str(tmp_path), **k)
        made.append(d)
        return d

    monkeypatch.setattr(vf.tempfile, "mkdtemp", spy)
    return made


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["not_a_video", "crash", "too_slow"])
async def test_the_working_files_go_even_when_the_look_fails(clips, monkeypatch, tmp_path, how):
    """Mutant: rmtree only on the success path. The old test only ever ran
    a clip that works."""
    made = _spy_workdirs(monkeypatch, tmp_path)
    data = clips["talk"]
    if how == "not_a_video":
        data = clips["talk"][:200]
    elif how == "crash":
        async def boom(*a, **k):
            raise RuntimeError("ffmpeg fell over")
        monkeypatch.setattr(vf, "_extract", boom)
    else:
        async def slow(*a, **k):
            await asyncio.sleep(30)
        monkeypatch.setattr(vf, "_extract", slow)
        monkeypatch.setattr(vf, "_TOTAL_TIMEOUT", 0.2)
    assert await asyncio.wait_for(
        vf.extract(data, max_frames=6, want_audio=True, max_seconds=300), 10) is None
    assert made and not any(os.path.exists(d) for d in made)


# ── the process wrapper, for real ────────────────────────────────────────────

def _running(needle: str) -> list[str]:
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if needle in l and "ps -eo" not in l]


@pytest.mark.asyncio
async def test_a_step_timeout_leaves_no_process_behind_through_the_real_wrapper(monkeypatch):
    """The old timeout test replaces create_subprocess_exec, so the sh wrapper
    is not in play. Mutant: `exec "$@"` -> `"$@"` leaves sh as the parent: the
    kill hits sh and the real command runs on, orphaned."""
    needle = f"sleep 31.{os.getpid()}"
    monkeypatch.setattr(vf, "_STEP_TIMEOUT", 0.4)
    with pytest.raises(asyncio.TimeoutError):
        await vf._run(["sleep", f"31.{os.getpid()}"])
    await asyncio.sleep(0.2)
    assert _running(needle) == []


@pytest.mark.asyncio
async def test_cancelling_a_look_kills_the_process_it_started():
    """The 90 s whole-video timeout arrives as a cancellation, not as the step
    TimeoutError. Mutant: `except BaseException` -> `except Exception` leaves
    ffmpeg running for its full CPU limit after the bot has given up."""
    needle = f"sleep 32.{os.getpid()}"
    task = asyncio.create_task(vf._run(["sleep", f"32.{os.getpid()}"]))
    await asyncio.sleep(0.4)
    assert _running(needle)                                     # it did start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert _running(needle) == []
