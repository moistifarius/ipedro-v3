"""Play a video backwards.

``ffmpeg``'s ``reverse`` filter holds the WHOLE clip in memory as decoded
frames, so a clip cannot just be handed to it: a minute of 1080p is several
gigabytes. This keeps the job inside a fixed budget instead, and refuses what
doesn't fit:

* only a real MP4/MOV or WebM/Matroska file, by its leading bytes, opened with
  the demuxer forced and only the ``file`` protocol allowed, with no stdin and
  none of the bot's secrets in its environment (all of it from
  ipedro/video_frames.py, whose limits on time, CPU, memory and output size
  apply here too, with a larger CPU and time allowance for the one thing this
  does that a frame grab doesn't, re-encoding);
* at most MAX_SECONDS long and MAX_INPUT_BYTES big (what Telegram lets a bot
  download), with a picture no bigger than 4096x2160;
* the frames ``reverse`` has to hold are at most FRAME_MEMORY bytes in all: the
  frame rate is capped at 30 and the picture is scaled down, in the filter
  itself (so a phone video that is stored rotated is scaled by what it shows),
  until frames x pixels fit. A 10 second 720p clip is untouched; a 30 second one
  comes out about 720x400;
* the sound is reversed with it (padded or trimmed to the picture first, so it
  stays in step), unless asked to leave it out;
* the copy carries none of the original's metadata (a phone video can hold the
  place it was shot), subtitles or data streams;
* it shares video_frames' two slots, so watching and reversing together can't
  run more than two ffmpeg jobs, and a job that can't get a turn in a few
  seconds is turned away, not queued.

Every failure is a :class:`Refused` with a short code, never a half result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import tempfile
from dataclasses import dataclass

from ipedro import video_frames as vf

log = logging.getLogger(__name__)

MAX_SECONDS = 30
MAX_INPUT_BYTES = 20_000_000          # what Telegram lets a bot download
MAX_OUTPUT_BYTES = 45_000_000         # what is worth sending back (the limit is 50 MB)
FPS_CAP = 30.0
MAX_WIDTH = 1280
FRAME_MEMORY = 400_000_000            # bytes of decoded frames `reverse` may hold
CRF = 26
_FFMPEG_CPU = 150                     # CPU seconds for the one re-encode
_FFMPEG_TIMEOUT = 100                 # wall seconds for it
_PROBE_TIMEOUT = vf._STEP_TIMEOUT
_TOTAL_TIMEOUT = 130                  # seconds, for the whole job


class Refused(Exception):
    """The reason a clip wasn't reversed. `code` is one of: unavailable,
    not-video, too-big, too-long, big-picture, busy, too-large-output, failed."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Probe:
    seconds: float
    has_audio: bool
    width: int
    height: int
    fps: float


@dataclass(frozen=True)
class Reversed:
    data: bytes             # an MP4
    seconds: float
    width: int
    height: int
    has_audio: bool


def _rate(text: object) -> float:
    """A frame rate from ffprobe's "30000/1001", or 0.0 when it isn't one."""
    try:
        top, _, bottom = str(text).partition("/")
        value = float(top) / (float(bottom) if bottom else 1.0)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


async def probe(path: str, fmt: str) -> Probe | None:
    """What ffprobe says of a file with a video stream, or None. The duration
    is the picture's (a file whose audio runs on past its last frame would
    otherwise be given time that has no picture)."""
    code, out, _ = await vf._run([
        "ffprobe", "-v", "error", "-protocol_whitelist", "file", "-f", fmt,
        "-of", "json", "-show_entries",
        "format=duration:stream=codec_type,width,height,duration,avg_frame_rate,r_frame_rate",
        "-i", path,
    ], timeout=_PROBE_TIMEOUT)
    if code != 0:
        return None
    try:
        info = json.loads(out)
        seconds = float((info.get("format") or {}).get("duration") or 0)
        streams = info.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            return None
        shown = video.get("duration")
        if shown not in (None, "N/A") and 0 < float(shown) < seconds:
            seconds = float(shown)
        fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
        return Probe(
            seconds=seconds, has_audio=any(s.get("codec_type") == "audio" for s in streams),
            width=int(video.get("width") or 0), height=int(video.get("height") or 0),
            fps=fps,
        )
    except (ValueError, TypeError):
        return None


def output_fps(source_fps: float) -> float:
    """The frame rate the copy is made at: the source's, never more than 30."""
    return min(source_fps, FPS_CAP) if source_fps > 0 else FPS_CAP


def pixel_budget(seconds: float, fps: float) -> int:
    """How many pixels a frame may have for the clip's frames to fit in
    FRAME_MEMORY (4:2:0 is a byte and a half a pixel)."""
    frames = max(1, math.ceil(seconds * fps) + 1)
    return max(int(FRAME_MEMORY / (frames * 1.5)), 2)


def video_filter(seconds: float, fps: float) -> str:
    """fps, then scale to the budget (by what the picture shows, so a rotated
    phone video isn't squashed), then reverse: the filter that holds the
    frames is last, so it holds the small ones."""
    budget = pixel_budget(seconds, fps)
    return (
        f"fps={fps:.3f},"
        f"scale=w='trunc(iw*min(min(1,sqrt({budget}/(iw*ih))),{MAX_WIDTH}/iw)/2)*2':h=-2,"
        "format=yuv420p,reverse"
    )


def audio_filter(seconds: float) -> str:
    """The sound, padded or trimmed to the picture's length, then reversed, so
    it comes out in step with the reversed picture."""
    return f"apad=whole_dur={seconds:.3f},atrim=end={seconds:.3f},areverse"


def command(path: str, fmt: str, out: str, info: Probe, *, mute: bool) -> list[str]:
    """The ffmpeg argv for one reversal (nothing from the file in it but paths
    made here)."""
    fps = output_fps(info.fps)
    argv = [
        "ffmpeg", *vf._COMMON, "-loglevel", "error", "-threads", "2",
        "-t", str(MAX_SECONDS + 1), "-f", fmt, "-i", path,
        "-map", "0:v:0",
    ]
    if info.has_audio and not mute:
        argv += ["-map", "0:a:0", "-af", audio_filter(info.seconds),
                 "-c:a", "aac", "-b:a", "96k", "-ac", "2"]
    else:
        argv += ["-an"]
    argv += [
        "-vf", video_filter(info.seconds, fps),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(CRF), "-threads", "2",
        "-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn",
        "-movflags", "+faststart", "-fs", "60000000", "-f", "mp4", "-y", out,
    ]
    return argv


async def reverse(
    data: bytes, *, mute: bool = False, max_seconds: int = MAX_SECONDS,
) -> Reversed:
    """`data` played backwards, as an MP4. Raises Refused."""
    fmt = vf.container(data)
    if fmt is None:
        raise Refused("not-video")
    if not vf.available():
        raise Refused("unavailable")
    if len(data) > MAX_INPUT_BYTES:
        raise Refused("too-big", f"{len(data)} bytes")
    gate = vf._gate()
    try:
        await asyncio.wait_for(gate.acquire(), vf._QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        raise Refused("busy") from None
    workdir = tempfile.mkdtemp(prefix="ipedro-reverse-")
    try:
        return await asyncio.wait_for(
            _reverse(workdir, data, fmt, mute, max_seconds), _TOTAL_TIMEOUT,
        )
    except Refused:
        raise
    except Exception as exc:
        log.info("reverse failed: %s: %s", type(exc).__name__, exc)
        raise Refused("failed") from exc
    finally:
        gate.release()
        await asyncio.to_thread(shutil.rmtree, workdir, True)


async def _reverse(
    workdir: str, data: bytes, fmt: str, mute: bool, max_seconds: int,
) -> Reversed:
    source, out = os.path.join(workdir, "input"), os.path.join(workdir, "out.mp4")
    await asyncio.to_thread(vf._write, source, data)
    info = await probe(source, fmt)
    if info is None or not (info.seconds > 0):
        raise Refused("not-video")
    if info.width * info.height > vf.MAX_PIXELS:
        raise Refused("big-picture", f"{info.width}x{info.height}")
    if info.seconds > max_seconds + 0.5:
        raise Refused("too-long", f"{info.seconds:.0f}")
    code, _, err = await vf._run(
        command(source, fmt, out, info, mute=mute),
        cpu=_FFMPEG_CPU, timeout=_FFMPEG_TIMEOUT,
    )
    if code != 0 or not os.path.exists(out):
        log.info("ffmpeg could not reverse a clip: exit %s: %s", code,
                 err.decode("utf-8", "replace")[-300:])
        raise Refused("failed")
    size = os.path.getsize(out)
    if size > MAX_OUTPUT_BYTES:
        raise Refused("too-large-output", f"{size} bytes")
    produced = await asyncio.to_thread(_read, out)
    if produced[4:8] != b"ftyp":
        raise Refused("failed")
    shown = await probe(out, "mov")
    if shown is None:
        raise Refused("failed")
    return Reversed(
        data=produced, seconds=shown.seconds, width=shown.width,
        height=shown.height, has_audio=shown.has_audio,
    )


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()
