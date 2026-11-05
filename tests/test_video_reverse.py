"""Playing a clip backwards, with the real ffmpeg where there is one.

What has to hold: the picture and the sound really come out reversed and stay
in step; the copy carries none of the original's metadata; the frames ffmpeg
must hold fit a fixed budget however big the source (a rotated phone video
included); anything that isn't a short real video is refused, with a code,
before ffmpeg is asked; and the job obeys the same limits and the same two
slots as watching a video.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
import time
from unittest.mock import AsyncMock

import pytest

from ipedro import video_frames as vf
from ipedro import video_reverse as vr

HAVE_FFMPEG = vf.available() and "libx264" in subprocess.run(
    ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True,
).stdout if vf.available() else False
real = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg with libx264")


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _clip(tmp_path, name="in.mp4", *, seconds=4, size="160x120", rate=10, audio=True,
          extra_out=(), source="ramp"):
    """A clip whose picture brightens with time (so its first frame is dark and
    its last bright) and whose sound is loud in the first half and silent in
    the second."""
    path = str(tmp_path / name)
    video = {
        "ramp": f"color=c=black:size={size}:rate={rate}:duration={seconds},"
                f"geq=lum='255*T/{seconds}':cb=128:cr=128",
        "test": f"testsrc2=duration={seconds}:size={size}:rate={rate}",
    }[source]
    inputs = ["-f", "lavfi", "-i", video]
    if audio:
        half = seconds / 2
        inputs += ["-f", "lavfi", "-i",
                   f"aevalsrc=if(lt(t\\,{half})\\,0.8*sin(2*PI*440*t)\\,0):s=44100:d={seconds}"]
    _ffmpeg(*inputs, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-crf", "38",
            *(["-c:a", "aac", "-shortest"] if audio else ["-an"]), *extra_out, path)
    return path


def _luma(path: str, at: float) -> float:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(at), "-i", path, "-frames:v", "1",
         "-vf", "scale=16:12,format=gray", "-f", "rawvideo", "-"],
        capture_output=True,
    ).stdout
    return sum(out) / len(out)


def _volume(path: str, start: float, length: float) -> float:
    err = subprocess.run(
        ["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(length), "-i", path,
         "-vn", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf)", err)
    return -200.0 if m is None or m.group(1) == "-inf" else float(m.group(1))


def _probe_json(path: str, entries: str) -> dict:
    return json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", "-show_entries", entries, path],
        capture_output=True, text=True,
    ).stdout)


async def _reverse_file(path: str, **kw) -> vr.Reversed:
    with open(path, "rb") as fh:
        return await vr.reverse(fh.read(), **kw)


def _save(tmp_path, made: vr.Reversed, name="out.mp4") -> str:
    path = str(tmp_path / name)
    with open(path, "wb") as fh:
        fh.write(made.data)
    return path


# ── really reversed ─────────────────────────────────────────────────────────

@real
@pytest.mark.asyncio
async def test_the_picture_comes_out_in_the_opposite_order(tmp_path):
    src = _clip(tmp_path)
    assert _luma(src, 0.05) < 40 and _luma(src, 3.85) > 200     # the source ramps up
    out = _save(tmp_path, await _reverse_file(src))
    assert _luma(out, 0.05) > 200 and _luma(out, 3.85) < 40     # the copy ramps down


@real
@pytest.mark.asyncio
async def test_the_sound_is_reversed_and_stays_in_step_with_the_picture(tmp_path):
    src = _clip(tmp_path)
    assert _volume(src, 0, 1.8) > -20 and _volume(src, 2.2, 1.8) < -60
    made = await _reverse_file(src)
    out = _save(tmp_path, made)
    assert made.has_audio
    assert _volume(out, 0, 1.8) < -60 and _volume(out, 2.2, 1.8) > -20
    assert abs(made.seconds - 4.0) < 0.35


@real
@pytest.mark.asyncio
async def test_mute_leaves_the_sound_out(tmp_path):
    made = await _reverse_file(_clip(tmp_path), mute=True)
    assert made.has_audio is False
    assert not _probe_json(_save(tmp_path, made), "stream=codec_type")["streams"][1:]


@real
@pytest.mark.asyncio
async def test_a_clip_with_no_sound_comes_out_with_none(tmp_path):
    made = await _reverse_file(_clip(tmp_path, audio=False))
    assert made.has_audio is False and made.data[4:8] == b"ftyp"


@real
@pytest.mark.asyncio
async def test_sound_longer_or_shorter_than_the_picture_stays_in_step(tmp_path):
    """The reversed sound is padded/trimmed to the picture first: otherwise
    sound that runs past the last frame would open the reversed clip."""
    path = str(tmp_path / "long_audio.mp4")
    _ffmpeg("-f", "lavfi", "-i", "color=c=gray:size=160x120:rate=10:duration=2",
            "-f", "lavfi", "-i", "aevalsrc=0.8*sin(2*PI*440*t):s=44100:d=5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-c:a", "aac", path)
    made = await _reverse_file(path)
    assert made.seconds < 2.6


@real
@pytest.mark.asyncio
async def test_the_copy_carries_none_of_the_originals_metadata(tmp_path):
    src = _clip(tmp_path, extra_out=("-metadata", "title=SECRET TITLE",
                                     "-metadata", "location=+51.5074-000.1278/",
                                     "-metadata", "comment=shot at home"))
    assert b"SECRET TITLE" in open(src, "rb").read()
    made = await _reverse_file(src)
    for needle in (b"SECRET TITLE", b"+51.5074", b"shot at home"):
        assert needle not in made.data


@real
@pytest.mark.asyncio
async def test_a_60_fps_source_is_made_at_30(tmp_path):
    src = _clip(tmp_path, rate=60, seconds=2, source="test")
    out = _save(tmp_path, await _reverse_file(src))
    rate = _probe_json(out, "stream=avg_frame_rate")["streams"][0]["avg_frame_rate"]
    top, _, bottom = rate.partition("/")
    assert abs(float(top) / float(bottom or 1) - 30) < 0.5


@real
@pytest.mark.asyncio
async def test_a_big_clip_is_scaled_to_fit_the_memory_budget_and_still_reverses(tmp_path):
    src = _clip(tmp_path, seconds=12, size="1920x1080", rate=30, audio=False, source="test")
    made = await _reverse_file(src)
    assert made.width <= vr.MAX_WIDTH and made.width % 2 == 0 and made.height % 2 == 0
    frames = math.ceil(12 * 30) + 1
    assert made.width * made.height <= vr.FRAME_MEMORY / (frames * 1.5) * 1.05
    assert abs(made.width / made.height - 16 / 9) < 0.05              # not squashed


@real
@pytest.mark.asyncio
async def test_a_small_short_clip_is_not_scaled_at_all(tmp_path):
    made = await _reverse_file(_clip(tmp_path, size="320x240"))
    assert (made.width, made.height) == (320, 240)


@real
@pytest.mark.asyncio
async def test_a_phone_video_stored_rotated_is_scaled_by_what_it_shows(tmp_path):
    flat = _clip(tmp_path, "flat.mp4", seconds=2, size="1920x1080", rate=30, audio=False, source="test")
    rotated = str(tmp_path / "rot.mp4")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", "90",
                    "-i", flat, "-c", "copy", rotated], check=True)
    made = await _reverse_file(rotated)
    assert made.height > made.width                                    # shown upright
    assert abs(made.height / made.width - 16 / 9) < 0.05


@real
@pytest.mark.asyncio
async def test_a_webm_is_opened_too(tmp_path):
    path = str(tmp_path / "in.webm")
    try:
        _ffmpeg("-f", "lavfi", "-i", "testsrc2=duration=1:size=160x120:rate=10",
                "-c:v", "libvpx", "-b:v", "200k", path)
    except subprocess.CalledProcessError:
        pytest.skip("this ffmpeg has no VP8 encoder")
    made = await _reverse_file(path)
    assert made.data[4:8] == b"ftyp"


# ── refusals, before ffmpeg is asked ────────────────────────────────────────

class _Spy:
    def __init__(self, monkeypatch):
        self.calls = []
        real_run = vf._run

        async def run(argv, **kw):
            self.calls.append((argv, kw))
            return await real_run(argv, **kw)

        monkeypatch.setattr(vf, "_run", run)


MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    b"", b"hello", b"#EXTM3U\n#EXT-X-STREAM-INF\nhttp://example.com/x.m3u8\n",
    b"ffconcat version 1.0\nfile '/etc/passwd'\n", b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,
])
async def test_what_is_not_a_video_container_never_reaches_ffmpeg(monkeypatch, data):
    spy = _Spy(monkeypatch)
    with pytest.raises(vr.Refused) as err:
        await vr.reverse(data)
    assert err.value.code == "not-video" and spy.calls == []


@pytest.mark.asyncio
async def test_a_file_over_the_download_limit_is_refused_before_ffmpeg(monkeypatch):
    spy = _Spy(monkeypatch)
    with pytest.raises(vr.Refused) as err:
        await vr.reverse(MP4 + b"\x00" * vr.MAX_INPUT_BYTES)
    assert err.value.code == "too-big" and spy.calls == []


@pytest.mark.asyncio
async def test_with_no_ffmpeg_it_says_so(monkeypatch):
    monkeypatch.setattr(vf, "available", lambda: False)
    with pytest.raises(vr.Refused) as err:
        await vr.reverse(MP4)
    assert err.value.code == "unavailable"


@real
@pytest.mark.asyncio
async def test_a_clip_that_is_too_long_is_refused_with_its_length(tmp_path, monkeypatch):
    src = _clip(tmp_path, seconds=33, size="64x48", rate=2, audio=False)
    spy = _Spy(monkeypatch)
    with pytest.raises(vr.Refused) as err:
        await _reverse_file(src)
    assert err.value.code == "too-long" and err.value.detail == "33"
    assert all(argv[0] != "ffmpeg" for argv, _ in spy.calls)          # only probed


@pytest.mark.asyncio
async def test_a_picture_too_big_to_decode_is_refused(monkeypatch):
    async def fake_probe(path, fmt):
        return vr.Probe(seconds=5.0, has_audio=False, width=8192, height=4320, fps=30.0)
    monkeypatch.setattr(vr, "probe", fake_probe)
    with pytest.raises(vr.Refused) as err:
        await vr.reverse(MP4)
    assert err.value.code == "big-picture"


@pytest.mark.asyncio
async def test_a_file_ffprobe_cannot_read_is_not_a_video(monkeypatch):
    monkeypatch.setattr(vr, "probe", AsyncMock(return_value=None))
    with pytest.raises(vr.Refused) as err:
        await vr.reverse(MP4)
    assert err.value.code == "not-video"


@pytest.mark.asyncio
async def test_a_zero_length_clip_is_not_a_video(monkeypatch):
    monkeypatch.setattr(vr, "probe", AsyncMock(return_value=vr.Probe(0.0, False, 64, 48, 10.0)))
    with pytest.raises(vr.Refused) as err:
        await vr.reverse(MP4)
    assert err.value.code == "not-video"


@pytest.mark.asyncio
async def test_a_failing_ffmpeg_is_a_failure_not_a_half_result(monkeypatch):
    monkeypatch.setattr(vr, "probe", AsyncMock(return_value=vr.Probe(5.0, True, 320, 240, 30.0)))
    monkeypatch.setattr(vf, "_run", AsyncMock(return_value=(1, b"", b"Invalid data found")))
    with pytest.raises(vr.Refused) as err:
        await vr.reverse(MP4)
    assert err.value.code == "failed"


@pytest.mark.asyncio
async def test_ffmpeg_that_exits_cleanly_but_wrote_nothing_is_a_failure(monkeypatch):
    monkeypatch.setattr(vr, "probe", AsyncMock(return_value=vr.Probe(5.0, False, 320, 240, 30.0)))
    monkeypatch.setattr(vf, "_run", AsyncMock(return_value=(0, b"", b"")))
    with pytest.raises(vr.Refused) as err:
        await vr.reverse(MP4)
    assert err.value.code == "failed"


@pytest.mark.asyncio
async def test_an_ffmpeg_that_times_out_is_a_failure(monkeypatch):
    monkeypatch.setattr(vr, "probe", AsyncMock(return_value=vr.Probe(5.0, False, 320, 240, 30.0)))
    monkeypatch.setattr(vf, "_run", AsyncMock(side_effect=asyncio.TimeoutError()))
    with pytest.raises(vr.Refused) as err:
        await vr.reverse(MP4)
    assert err.value.code == "failed"


@real
@pytest.mark.asyncio
async def test_a_copy_too_big_to_send_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "MAX_OUTPUT_BYTES", 100)
    with pytest.raises(vr.Refused) as err:
        await _reverse_file(_clip(tmp_path))
    assert err.value.code == "too-large-output"


@pytest.mark.asyncio
async def test_a_result_that_is_not_an_mp4_is_a_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(vr, "probe", AsyncMock(return_value=vr.Probe(5.0, False, 320, 240, 30.0)))

    async def fake_run(argv, **kw):
        with open(argv[-1], "wb") as fh:
            fh.write(b"not a video at all")
        return 0, b"", b""

    monkeypatch.setattr(vf, "_run", fake_run)
    with pytest.raises(vr.Refused) as err:
        await vr.reverse(MP4)
    assert err.value.code == "failed"


# ── the slots, the limits and the clean-up ──────────────────────────────────

@pytest.mark.asyncio
async def test_when_both_slots_are_busy_it_says_so_instead_of_queueing(monkeypatch):
    monkeypatch.setattr(vf, "_QUEUE_TIMEOUT", 0.05)
    gate = vf._gate()
    await gate.acquire()
    await gate.acquire()
    try:
        with pytest.raises(vr.Refused) as err:
            await vr.reverse(MP4)
        assert err.value.code == "busy"
    finally:
        gate.release()
        gate.release()


@pytest.mark.asyncio
async def test_the_slot_and_the_work_directory_are_given_back_even_on_failure(monkeypatch):
    seen = {}
    real_mkdtemp = vr.tempfile.mkdtemp

    def mkdtemp(*a, **k):
        seen["dir"] = real_mkdtemp(*a, **k)
        return seen["dir"]

    monkeypatch.setattr(vr.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(vr, "probe", AsyncMock(return_value=None))
    gate = vf._gate()
    for _ in range(3):
        with pytest.raises(vr.Refused):
            await vr.reverse(MP4)
    assert not os.path.exists(seen["dir"])
    assert gate._value == 2                                            # both slots free


@pytest.mark.asyncio
async def test_the_encode_gets_a_bigger_allowance_than_a_frame_grab_and_the_probe_does_not(monkeypatch):
    calls = []

    async def fake_run(argv, **kw):
        calls.append((argv[0], kw))
        if argv[0] == "ffprobe":
            return 0, json.dumps({"format": {"duration": "5.0"}, "streams": [
                {"codec_type": "video", "width": 320, "height": 240,
                 "avg_frame_rate": "30/1"}]}).encode(), b""
        with open(argv[-1], "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 50)
        return 0, b"", b""

    monkeypatch.setattr(vf, "_run", fake_run)
    await vr.reverse(MP4)
    probe_kw = [kw for name, kw in calls if name == "ffprobe"][0]
    encode_kw = [kw for name, kw in calls if name == "ffmpeg"][0]
    assert encode_kw == {"cpu": vr._FFMPEG_CPU, "timeout": vr._FFMPEG_TIMEOUT}
    assert "cpu" not in probe_kw and vr._FFMPEG_CPU > 60


@pytest.mark.asyncio
async def test_the_run_wrapper_raises_cpu_only_for_the_job_that_asks(monkeypatch):
    seen = {}

    async def fake_exec(*argv, **kw):
        seen["argv"] = argv

        class P:
            returncode = 0

            async def communicate(self):
                return b"", b""
        return P()

    monkeypatch.setattr(vf.asyncio, "create_subprocess_exec", fake_exec)
    await vf._run(["ffmpeg"])
    assert seen["argv"][2] == vf._ULIMITS and "cap -t 60;" in seen["argv"][2]
    await vf._run(["ffmpeg"], cpu=150)
    assert "cap -t 150;" in seen["argv"][2] and "cap -v 1572864" in seen["argv"][2]
    assert "cap -f 131072" in seen["argv"][2]                         # memory and size never raised


# ── the command itself ──────────────────────────────────────────────────────

INFO = vr.Probe(seconds=10.0, has_audio=True, width=1280, height=720, fps=29.97)


def test_the_command_is_built_from_paths_and_numbers_made_here():
    argv = vr.command("/w/input", "mov", "/w/out.mp4", INFO, mute=False)
    assert argv[0] == "ffmpeg"
    assert argv[argv.index("-protocol_whitelist") + 1] == "file"
    assert argv[argv.index("-f") + 1] == "mov" and argv[argv.index("-i") + 1] == "/w/input"
    assert argv[argv.index("-t") + 1] == str(vr.MAX_SECONDS + 1)
    assert "-nostdin" in argv and argv[argv.index("-map_metadata") + 1] == "-1"
    assert {"-sn", "-dn"} <= set(argv) and argv[-1] == "/w/out.mp4"
    assert argv.index("-f") < argv.index("-i")                         # the demuxer is forced first
    assert "-af" in argv and "aac" in argv and "-an" not in argv
    assert "-fs" in argv and int(argv[argv.index("-fs") + 1]) <= 64 * 1024 * 1024
    assert "/w/input" not in " ".join(a for a in argv if a != "/w/input")


def test_mute_and_no_sound_both_drop_audio_handling():
    for argv in (vr.command("/i", "mov", "/o", INFO, mute=True),
                 vr.command("/i", "mov", "/o", vr.Probe(5.0, False, 64, 48, 10.0), mute=False)):
        assert "-an" in argv and "-af" not in argv and "aac" not in argv


def test_the_frame_rate_is_the_sources_but_never_over_30():
    assert vr.output_fps(24.0) == 24.0 and vr.output_fps(60.0) == 30.0
    assert vr.output_fps(0.0) == 30.0 and vr.output_fps(-5.0) == 30.0
    assert "fps=29.970" in vr.video_filter(10.0, vr.output_fps(29.97))
    assert "fps=30.000" in vr.video_filter(10.0, vr.output_fps(120.0))


def test_the_pixel_budget_shrinks_as_the_clip_grows_and_holds_the_memory_limit():
    short, long = vr.pixel_budget(5, 30), vr.pixel_budget(30, 30)
    assert short > long > 0
    assert long * 1.5 * (30 * 30 + 1) <= vr.FRAME_MEMORY
    assert vr.pixel_budget(0.01, 30) * 1.5 * 2 <= vr.FRAME_MEMORY * 5   # a tiny clip: fine
    assert vr.pixel_budget(10, 30) >= 1280 * 720 * 0.9                  # 10 s of 720p fits whole


def test_the_filter_holds_its_frames_last_and_scales_by_what_the_picture_shows():
    graph = vr.video_filter(10.0, 30.0)
    assert graph.endswith(",reverse") and graph.index("fps=") < graph.index("scale=") < graph.index("reverse")
    assert "iw*ih" in graph and "format=yuv420p" in graph and f"{vr.MAX_WIDTH}/iw" in graph
    assert "h=-2" in graph                                              # an even height, same shape


def test_the_sound_is_padded_trimmed_then_reversed():
    graph = vr.audio_filter(7.25)
    assert graph == "apad=whole_dur=7.250,atrim=end=7.250,areverse"


@pytest.mark.parametrize("text, rate", [
    ("30000/1001", 29.97), ("30/1", 30.0), ("25", 25.0), ("0/0", 0.0), ("abc", 0.0),
    ("inf", 0.0), ("-3/1", 0.0), (None, 0.0), ("1/0", 0.0), ("nan", 0.0),
])
def test_a_frame_rate_is_read_or_is_nothing(text, rate):
    assert abs(vr._rate(text) - rate) < 0.01


def test_the_numbers_are_the_documented_ones():
    assert vr.MAX_SECONDS == 30 and vr.MAX_INPUT_BYTES == 20_000_000
    assert vr.FPS_CAP == 30.0 and vr.FRAME_MEMORY == 400_000_000
    assert vr.MAX_OUTPUT_BYTES < 50_000_000


# ── what the mutation sweep found unpinned ──────────────────────────────────

@real
@pytest.mark.asyncio
async def test_a_short_wide_clip_is_still_capped_to_the_maximum_width(tmp_path):
    """Two seconds fits the frame budget even at 1080p, so only the width cap
    can bring it down."""
    src = _clip(tmp_path, seconds=2, size="1920x1080", rate=30, audio=False, source="test")
    made = await _reverse_file(src)
    assert 1000 < made.width <= 1280                      # the documented cap, not the constant
    assert abs(made.width / made.height - 16 / 9) < 0.05


@real
@pytest.mark.asyncio
async def test_a_chapter_in_the_source_does_not_come_along(tmp_path):
    chapters = tmp_path / "chapters.txt"
    chapters.write_text(
        ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=2000\ntitle=PRIVATE CHAPTER\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=2000\nEND=4000\ntitle=SECOND ONE\n")
    plain = _clip(tmp_path, "plain.mp4")
    src = str(tmp_path / "chaptered.mp4")
    _ffmpeg("-i", plain, "-i", str(chapters), "-map_metadata", "1", "-map_chapters", "1", "-c", "copy", src)
    assert _probe_json(src, "chapter=id")["chapters"], "the source has chapters"
    made = await _reverse_file(src)
    assert b"PRIVATE CHAPTER" not in made.data
    assert not _probe_json(_save(tmp_path, made), "chapter=id")["chapters"]


@pytest.mark.asyncio
async def test_the_frame_rate_falls_back_to_the_other_field_when_the_average_is_unknown(monkeypatch):
    async def fake_run(argv, **kw):
        return 0, json.dumps({"format": {"duration": "4.0"}, "streams": [
            {"codec_type": "video", "width": 320, "height": 240,
             "avg_frame_rate": "0/0", "r_frame_rate": "25/1"}]}).encode(), b""
    monkeypatch.setattr(vf, "_run", fake_run)
    info = await vr.probe("/x", "mov")
    assert info.fps == 25.0 and info.seconds == 4.0 and not info.has_audio


@pytest.mark.asyncio
async def test_an_ffmpeg_that_failed_is_a_failure_even_if_it_left_a_file_behind(monkeypatch):
    monkeypatch.setattr(vr, "probe", AsyncMock(return_value=vr.Probe(5.0, False, 320, 240, 30.0)))

    async def fake_run(argv, **kw):
        with open(argv[-1], "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" + b"x" * 40)       # looks fine…
        return 1, b"", b"Conversion failed!"                       # …but ffmpeg said no

    monkeypatch.setattr(vf, "_run", fake_run)
    with pytest.raises(vr.Refused) as err:
        await vr.reverse(MP4)
    assert err.value.code == "failed"
