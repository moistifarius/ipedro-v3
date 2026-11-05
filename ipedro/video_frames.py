"""Stills and speech out of a video, so the bot can describe it.

A vision model reads pictures and a transcriber reads audio; a video is
neither. This turns one into a handful of evenly spaced frames and, when
asked, the audio track as a small mp3 (``vision.py`` does the describing).

Whatever a member posts ends up in front of ffmpeg, which is a large parser
of untrusted bytes, so this is deliberately narrow:

* only an MP4/MOV or a WebM/Matroska file, by its leading bytes. A text file
  that merely *calls itself* a video (an HLS or concat playlist is the classic
  one: ffmpeg will follow it to other files and other hosts) is refused
  before ffmpeg sees it;
* the demuxer is forced and only the ``file`` protocol is allowed, so nothing
  inside a file can reach another file or the network anyway;
* each process runs with a time limit, a CPU limit, a memory limit and a
  limit on what it may write, with no stdin and an environment that carries
  none of the bot's secrets;
* the REAL picture size (what ffprobe reads from the stream, not what the
  container claims) is capped: a 750 KB clip of 16000x16000 black frames
  decodes into gigabytes, and the memory limit is the backstop, not the gate;
* two videos at a time, and a video that can't get a turn within a few
  seconds is turned away, not queued behind someone's slow file; size and
  length capped by the caller.

Every failure returns None, never an exception: a video we couldn't open is a
missing sentence, not a dropped message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

log = logging.getLogger(__name__)

FRAME_WIDTH = 768                 # px; wider is wasted on a cheap vision model
_MAX_FRAME_BYTES = 3_500_000      # under every provider's per-image ceiling
AUDIO_SECONDS = 300               # the first five minutes of speech is plenty
# Below this mean level (dB) a track is silence or hiss, and a transcriber
# invents words for silence ("Thank you.", "Subtitles by...").
SILENCE_DB = -45.0

_STEP_TIMEOUT = 30                # seconds, per ffmpeg/ffprobe process
_TOTAL_TIMEOUT = 90               # seconds, for one whole video
_QUEUE_TIMEOUT = 15               # seconds to wait for one of the two slots
# 4096x2160. A phone's 4K is under this; an 8K or a bomb is not worth the
# memory, and a model reads frames scaled to 768 wide anyway.
MAX_PIXELS = 4096 * 2160
_slots_loop: asyncio.AbstractEventLoop | None = None
_slots: asyncio.Semaphore | None = None

_MEAN_VOLUME = re.compile(r"mean_volume:\s*(-?[\d.]+|-inf)\s*dB")
_SAFE_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C"}


@dataclass(frozen=True)
class VideoParts:
    seconds: float
    frames: list[bytes]           # JPEGs, in order
    audio: bytes | None           # mp3, only when there is audible audio
    had_audio_track: bool


def _gate() -> asyncio.Semaphore:
    """Two videos at a time. Made on first use in the running loop: a
    semaphore that contended once is bound to that loop for good."""
    global _slots, _slots_loop
    loop = asyncio.get_running_loop()
    if _slots is None or _slots_loop is not loop:
        _slots, _slots_loop = asyncio.Semaphore(2), loop
    return _slots


def available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def container(data: bytes) -> str | None:
    """The demuxer to force for these bytes, or None if they are not a video
    container we are willing to open."""
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "mov"                              # mp4, mov, 3gp
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"                         # webm, mkv
    return None


def frame_count(seconds: float, wanted: int) -> int:
    """How many frames to take: a few for a clip, more for a longer one,
    never more than `wanted`."""
    n = 4 if seconds <= 8 else 6 if seconds <= 30 else 8
    return max(1, min(wanted, n))


def timestamps(seconds: float, n: int) -> list[float]:
    """The middle of each of n equal slices: never frame zero, which is so
    often a black or title card."""
    return [seconds * (i + 0.5) / n for i in range(n)]


# Set by the shell that then becomes ffmpeg, rather than by a hook run in the
# forked child: preexec_fn is documented as unsafe in a process with threads
# (this one has a pool), where it can deadlock. Generous for ffmpeg, fatal for
# a decompression bomb. dash's `ulimit` reports failure on stderr but exits 0,
# so each limit is read back: unlimited, or not below what we asked, and the
# command does not run (126) rather than running without it. KiB, seconds,
# 512-byte blocks.
_ULIMITS = (
    'cap() { ulimit "$1" "$2" 2>/dev/null; v=$(ulimit "$1"); '
    '[ "$v" != unlimited ] && [ "$v" -le "$2" ] 2>/dev/null || exit 126; }; '
    'cap -v 1572864; cap -t 60; cap -f 131072; exec "$@"'
)


async def _run(
    argv: list[str], *, cpu: int = 60, timeout: float | None = None,
) -> tuple[int, bytes, bytes]:
    """Run `argv` under the limits above. `cpu` (seconds) and `timeout` (wall
    seconds) are for the one job that legitimately needs more than a frame
    grab, re-encoding a clip (ipedro/video_reverse.py); everything else gets
    the defaults. The memory and file-size limits are never raised."""
    script = _ULIMITS if cpu == 60 else _ULIMITS.replace("cap -t 60;", f"cap -t {int(cpu)};")
    proc = await asyncio.create_subprocess_exec(
        "sh", "-c", script, "limited", *argv,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=_SAFE_ENV,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout or _STEP_TIMEOUT)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    return proc.returncode or 0, out, err


_COMMON = ["-nostdin", "-hide_banner", "-protocol_whitelist", "file"]


async def _probe(path: str, fmt: str) -> tuple[float, bool] | None:
    """(seconds, has an audio track) for a file with a video stream of a size
    worth decoding, else None. The duration is the picture's: a file whose
    audio runs past its last frame would otherwise have frames sampled from
    time that has no picture."""
    code, out, _ = await _run([
        "ffprobe", "-v", "error", "-protocol_whitelist", "file", "-f", fmt,
        "-of", "json", "-show_entries",
        "format=duration:stream=codec_type,width,height,duration",
        "-i", path,
    ])
    if code != 0:
        return None
    try:
        info = json.loads(out)
        seconds = float((info.get("format") or {}).get("duration") or 0)
        streams = info.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            return None
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        shown = video.get("duration")
        if shown not in (None, "N/A") and 0 < float(shown) < seconds:
            seconds = float(shown)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
    except (ValueError, TypeError):
        return None
    if not (seconds > 0) or width * height > MAX_PIXELS:
        if width * height > MAX_PIXELS:
            log.info("video too big to decode: %sx%s", width, height)
        return None
    return seconds, has_audio


async def _frame(path: str, fmt: str, at: float) -> bytes | None:
    code, out, _ = await _run([
        "ffmpeg", *_COMMON, "-loglevel", "error", "-threads", "1",
        "-ss", f"{at:.2f}", "-f", fmt, "-i", path, "-frames:v", "1",
        "-vf", f"scale='min({FRAME_WIDTH},iw)':-2", "-q:v", "5",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-",
    ])
    if code != 0 or not out.startswith(b"\xff\xd8\xff") or len(out) > _MAX_FRAME_BYTES:
        return None
    return out


async def _audio(path: str, fmt: str) -> bytes | None:
    """The audio as a small mp3, or None when there is none worth hearing."""
    code, out, err = await _run([
        "ffmpeg", *_COMMON, "-loglevel", "info", "-threads", "1", "-f", fmt,
        "-i", path, "-vn", "-sn", "-dn", "-t", str(AUDIO_SECONDS), "-ac", "1",
        "-ar", "16000", "-af", "volumedetect", "-c:a", "libmp3lame",
        "-b:a", "32k", "-f", "mp3", "-",
    ])
    if code != 0 or not out:
        return None
    m = _MEAN_VOLUME.search(err.decode("utf-8", "replace"))
    if m is None or m.group(1) == "-inf" or float(m.group(1)) < SILENCE_DB:
        return None
    return out


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


async def extract(
    data: bytes, *, max_frames: int, want_audio: bool, max_seconds: int,
) -> VideoParts | None:
    """Frames (and the speech track) of `data`, or None if it isn't a video
    we can or should open."""
    fmt = container(data)
    if fmt is None or max_frames < 1 or not available():
        return None
    gate = _gate()
    try:
        await asyncio.wait_for(gate.acquire(), _QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        log.info("video skipped: no free slot within %ss", _QUEUE_TIMEOUT)
        return None
    workdir = tempfile.mkdtemp(prefix="ipedro-video-")
    try:
        return await asyncio.wait_for(
            _extract(workdir, data, fmt, max_frames, want_audio, max_seconds),
            _TOTAL_TIMEOUT,
        )
    except Exception as exc:
        log.info("video extraction failed: %s: %s", type(exc).__name__, exc)
        return None
    finally:
        gate.release()
        await asyncio.to_thread(shutil.rmtree, workdir, True)


async def _extract(
    workdir: str, data: bytes, fmt: str, max_frames: int, want_audio: bool,
    max_seconds: int,
) -> VideoParts | None:
    path = os.path.join(workdir, "input")
    await asyncio.to_thread(_write, path, data)
    probed = await _probe(path, fmt)
    if probed is None:
        return None
    seconds, has_audio = probed
    if seconds > max_seconds:
        log.info("video too long to watch: %.0fs > %ss", seconds, max_seconds)
        return None
    frames: list[bytes] = []
    for at in timestamps(seconds, frame_count(seconds, max_frames)):
        frame = await _frame(path, fmt, at)
        if frame is None:
            # Seeking lands on the frame AFTER the time asked for, so a clip
            # with one still picture, or a slow frame rate, has none there.
            frame = await _frame(path, fmt, 0.0)
        if frame is not None and (not frames or frame != frames[-1]):
            frames.append(frame)
    if not frames:
        return None
    audio = await _audio(path, fmt) if (want_audio and has_audio) else None
    return VideoParts(
        seconds=seconds, frames=frames, audio=audio, had_audio_track=has_audio,
    )
