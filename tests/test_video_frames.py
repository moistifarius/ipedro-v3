"""Frames and speech out of a video (ipedro/video_frames.py).

Most of these run the real ffmpeg on small clips made on the spot, because
what is being pinned is the command line (a wrong flag fails quietly into
"None"). The ones about what is REFUSED need no ffmpeg at all: they prove
the bytes never get near it.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from ipedro import video_frames as vf

needs_ffmpeg = pytest.mark.skipif(
    not vf.available(), reason="ffmpeg/ffprobe not on PATH")


def _make(path, *inputs, args=()):
    cmd = ["ffmpeg", "-loglevel", "error", "-y"]
    for i in inputs:
        cmd += ["-f", "lavfi", "-i", i]
    subprocess.run([*cmd, *args, str(path)], check=True)
    return path.read_bytes()


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    if not vf.available():
        return {}
    d = tmp_path_factory.mktemp("clips")
    h264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    return {
        "talk": _make(d / "talk.mp4", "testsrc=duration=12:size=640x360:rate=24",
                      "sine=frequency=440:duration=12",
                      args=[*h264, "-c:a", "aac", "-shortest"]),
        "silent": _make(d / "silent.mp4", "testsrc=duration=5:size=320x240:rate=24",
                        args=h264),
        "quiet": _make(d / "quiet.mp4", "testsrc=duration=6:size=320x240:rate=24",
                       "anullsrc=r=16000:cl=mono",
                       args=[*h264, "-c:a", "aac", "-t", "6"]),
        "tall": _make(d / "tall.mp4", "testsrc=duration=3:size=1080x1920:rate=24",
                      args=h264),
        "webm": _make(d / "clip.webm", "testsrc=duration=4:size=320x240:rate=24",
                      args=["-c:v", "libvpx", "-b:v", "200k"]),
        "audio_only": _make(d / "song.mp4", "sine=frequency=330:duration=5",
                            args=["-c:a", "aac"]),
    }


# ── what is a video we will open ─────────────────────────────────────────────

@pytest.mark.parametrize("data, fmt", [
    (b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16, "mov"),
    (b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 16, "mov"),
    (b"\x1a\x45\xdf\xa3" + b"\x00" * 16, "matroska"),
    (b"#EXTM3U\n#EXT-X-VERSION:3\nfile:///etc/passwd\n", None),
    (b"ffconcat version 1.0\nfile /etc/passwd\n", None),
    (b"<?xml version='1.0'?><MPD/>", None),
    (b"GIF89a" + b"\x00" * 16, None),
    (b"", None),
    (b"ftyp", None),
])
def test_only_real_containers_are_opened(data, fmt):
    assert vf.container(data) == fmt


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    b"#EXTM3U\n#EXTINF:5,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n",
    b"ffconcat version 1.0\nfile /etc/passwd\n",
    b"x" * 2000,
])
async def test_a_file_that_only_calls_itself_a_video_never_reaches_ffmpeg(monkeypatch, data):
    async def boom(argv):
        raise AssertionError(f"ffmpeg was run: {argv}")

    monkeypatch.setattr(vf, "_run", boom)
    assert await vf.extract(data, max_frames=6, want_audio=True, max_seconds=300) is None


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_playlist_inside_a_real_looking_header_still_cannot_read_files(tmp_path):
    """Even if the header check were fooled, the demuxer is forced and only
    the file protocol is allowed. A header followed by a playlist is not a
    parseable mp4, so nothing comes back."""
    sneaky = b"\x00\x00\x00\x18ftypmp42" + b"#EXTM3U\n#EXTINF:5,\nfile:///etc/passwd\n"
    assert await vf.extract(sneaky, max_frames=6, want_audio=True, max_seconds=300) is None


# ── how many frames, and where ───────────────────────────────────────────────

@pytest.mark.parametrize("seconds, wanted, n", [
    (3, 6, 4), (8, 6, 4), (9, 6, 6), (30, 6, 6), (31, 8, 8), (600, 20, 8),
    (3, 2, 2), (100, 1, 1), (50, 0, 1),
])
def test_frame_counts(seconds, wanted, n):
    assert vf.frame_count(seconds, wanted) == n


def test_frames_come_from_the_middle_of_equal_slices():
    ts = vf.timestamps(12.0, 4)
    assert ts == pytest.approx([1.5, 4.5, 7.5, 10.5])
    assert vf.timestamps(10.0, 1) == pytest.approx([5.0])
    assert min(vf.timestamps(100.0, 8)) > 0          # never the (often black) first frame


# ── real clips ───────────────────────────────────────────────────────────────

@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_clip_with_sound_gives_frames_and_audio(clips):
    parts = await vf.extract(clips["talk"], max_frames=6, want_audio=True, max_seconds=300)
    assert parts is not None
    assert parts.seconds == pytest.approx(12.0, abs=0.5)
    assert len(parts.frames) == 6
    assert all(f.startswith(b"\xff\xd8\xff") for f in parts.frames)    # JPEGs
    assert parts.audio and parts.had_audio_track
    assert parts.audio[:3] == b"ID3" or parts.audio[0] == 0xFF         # an mp3


@needs_ffmpeg
@pytest.mark.asyncio
async def test_frames_are_different_moments_not_one_repeated(clips):
    parts = await vf.extract(clips["talk"], max_frames=6, want_audio=False, max_seconds=300)
    assert len(set(parts.frames)) == len(parts.frames)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_audio_is_only_pulled_when_asked_for(clips):
    parts = await vf.extract(clips["talk"], max_frames=4, want_audio=False, max_seconds=300)
    assert parts.audio is None and parts.had_audio_track
    assert len(parts.frames) == 4


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_silent_video_has_no_track_to_transcribe(clips):
    parts = await vf.extract(clips["silent"], max_frames=6, want_audio=True, max_seconds=300)
    assert parts.audio is None and parts.had_audio_track is False
    assert len(parts.frames) == 4                       # a 5-second clip


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_track_of_silence_is_not_sent_to_a_transcriber(clips):
    """A transcriber makes words up for silence."""
    parts = await vf.extract(clips["quiet"], max_frames=6, want_audio=True, max_seconds=300)
    assert parts.had_audio_track is True and parts.audio is None
    assert parts.frames


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_portrait_video_is_scaled_to_a_sensible_width(clips):
    parts = await vf.extract(clips["tall"], max_frames=2, want_audio=False, max_seconds=300)
    assert parts and all(len(f) < 3_500_000 for f in parts.frames)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", "-f", "image2pipe", "-i", "-"],
        input=parts.frames[0], capture_output=True, check=True).stdout.decode().strip()
    width, height = map(int, out.split(",")[:2])
    assert width == vf.FRAME_WIDTH and height > width        # 768 wide, still portrait


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_webm_works_too(clips):
    parts = await vf.extract(clips["webm"], max_frames=6, want_audio=True, max_seconds=300)
    assert parts and len(parts.frames) == 4 and parts.audio is None


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_file_with_no_picture_is_not_a_video(clips):
    assert await vf.extract(clips["audio_only"], max_frames=6, want_audio=True, max_seconds=300) is None


@needs_ffmpeg
@pytest.mark.asyncio
async def test_too_long_is_refused_by_the_real_duration_not_the_claimed_one(clips):
    assert await vf.extract(clips["talk"], max_frames=6, want_audio=True, max_seconds=11) is None
    assert await vf.extract(clips["talk"], max_frames=6, want_audio=True, max_seconds=12) is not None


@needs_ffmpeg
@pytest.mark.asyncio
async def test_truncated_garbage_with_a_real_header_is_none_not_an_exception(clips):
    broken = clips["talk"][:200]
    assert await vf.extract(broken, max_frames=6, want_audio=True, max_seconds=300) is None


@pytest.mark.asyncio
async def test_without_frames_wanted_it_does_nothing():
    assert await vf.extract(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64,
                            max_frames=0, want_audio=True, max_seconds=300) is None


@needs_ffmpeg
@pytest.mark.asyncio
async def test_the_working_files_are_cleaned_up(clips, monkeypatch, tmp_path):
    made = []
    real = vf.tempfile.mkdtemp

    def spy(*a, **k):
        d = real(*a, dir=str(tmp_path), **k)
        made.append(d)
        return d

    monkeypatch.setattr(vf.tempfile, "mkdtemp", spy)
    await vf.extract(clips["silent"], max_frames=2, want_audio=False, max_seconds=300)
    assert made and not any(__import__("os").path.exists(d) for d in made)


# ── containment ──────────────────────────────────────────────────────────────

def test_ffmpeg_runs_with_nothing_of_the_bots_environment(monkeypatch):
    assert set(vf._SAFE_ENV) == {"PATH", "LANG"}


@pytest.mark.asyncio
async def test_every_process_is_wrapped_in_the_limits_and_has_no_stdin(monkeypatch):
    seen = {}
    real = asyncio.create_subprocess_exec

    async def spy(*argv, **kw):
        seen.update(kw)
        seen["argv"] = argv
        return await real("true", **{k: v for k, v in kw.items()})

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    await vf._run(["ffmpeg", "-version"])
    assert seen["argv"][:3] == ("sh", "-c", vf._ULIMITS)
    assert seen["argv"][-2:] == ("ffmpeg", "-version")
    assert seen["stdin"] == asyncio.subprocess.DEVNULL
    assert seen["env"] == vf._SAFE_ENV
    assert "preexec_fn" not in seen                  # unsafe in a threaded process


@pytest.mark.asyncio
async def test_the_limits_really_are_in_force_inside_the_process():
    code, out, _ = await vf._run(["sh", "-c", "ulimit -v; ulimit -t; ulimit -f"])
    assert code == 0
    mem, cpu, fsize = out.decode().split()
    assert (mem, cpu, fsize) == ("1572864", "60", "131072")


@pytest.mark.asyncio
async def test_a_limit_that_cannot_be_set_means_the_command_does_not_run(monkeypatch):
    """dash says so on stderr and exits 0, so the limit is read back. Here
    ulimit is made a no-op, as on a platform without the limit."""
    monkeypatch.setattr(vf, "_ULIMITS", "ulimit() { return 0; }; " + vf._ULIMITS)
    code, out, _ = await vf._run(["sh", "-c", "echo ran"])
    assert code == 126 and b"ran" not in out


@pytest.mark.asyncio
async def test_a_tighter_limit_already_in_force_is_accepted(monkeypatch):
    monkeypatch.setattr(vf, "_ULIMITS", "ulimit -v 1000000; " + vf._ULIMITS)
    code, out, _ = await vf._run(["sh", "-c", "echo ran; ulimit -v"])
    assert code == 0 and out.split() == [b"ran", b"1000000"]


@pytest.mark.asyncio
async def test_a_command_that_exceeds_its_memory_limit_dies_instead_of_eating_the_machine():
    code, _, _ = await vf._run([
        "python3", "-c", "x = bytearray(4 * 1024**3); print('allocated')"])
    assert code != 0


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_stuck_process_is_killed_at_the_step_timeout(monkeypatch):
    monkeypatch.setattr(vf, "_STEP_TIMEOUT", 0.2)
    started = []
    real = asyncio.create_subprocess_exec

    async def sleeper(*argv, **kw):
        proc = await real("sleep", "30", **{k: v for k, v in kw.items() if k != "preexec_fn"})
        started.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", sleeper)
    with pytest.raises(asyncio.TimeoutError):
        await vf._run(["ffmpeg"])
    await asyncio.sleep(0.05)
    assert started[0].returncode is not None             # reaped, not left running


@pytest.mark.asyncio
async def test_two_videos_at_a_time_and_not_more():
    running = peak = 0

    async def fake(workdir, data, fmt, frames, audio, secs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return None

    real = vf._extract
    vf._extract = fake
    try:
        vid = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
        await asyncio.gather(*[
            vf.extract(vid, max_frames=2, want_audio=False, max_seconds=60)
            for _ in range(6)
        ])
    finally:
        vf._extract = real
    assert peak == 2 if vf.available() else peak == 0


@pytest.mark.asyncio
async def test_the_gate_belongs_to_the_running_loop():
    a = vf._gate()
    assert vf._gate() is a
    # a new loop (the next test, a restart in-process) gets its own
    other = await asyncio.to_thread(lambda: asyncio.run(_gate_in_new_loop()))
    assert other is not a


async def _gate_in_new_loop():
    return vf._gate()
