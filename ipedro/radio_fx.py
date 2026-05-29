"""Radio-transmission audio effect for /ether voice broadcasts.

Takes arbitrary input audio (a Telegram voice note's OGG/Opus, or TTS
output) and runs it through an ffmpeg filtergraph that makes it sound
like a faint, far-away radio signal: a tight bandpass, bit-crush
distortion, amplitude tremolo (signal fading in and out), and a layer
of mixed-in static. Output is mono OGG/Opus, ready for ``send_voice``.

Everything is done in a single ffmpeg invocation over pipes — no temp
files, no PCM round-trip in Python. ``intensity`` ∈ [0,1] scales the
degradation the same way the text garble does, so a transmission can be
anything from "slightly distant" to "barely punching through the noise".

If ffmpeg isn't installed (``ffmpeg_available()`` is False) the caller
should fall back to a text broadcast.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger(__name__)

_FFMPEG_TIMEOUT_SECONDS = 60


def ffmpeg_available() -> bool:
    """True iff an ffmpeg binary is on PATH."""
    return shutil.which("ffmpeg") is not None


def _build_filtergraph(intensity: float) -> str:
    """Build the ffmpeg -filter_complex string for the given intensity.

    Input 0 = the voice audio, input 1 = an infinite pink-noise source.
    Higher intensity tightens the band, deepens the crush, and raises the
    static level. ``amix=duration=first`` trims the (infinite) noise to
    the voice length.
    """
    i = max(0.0, min(1.0, intensity))
    # Tighten the passband as intensity climbs (more "telephone"/distant).
    high_pass = int(250 + 250 * i)     # 250 → 500 Hz
    low_pass = int(3400 - 1200 * i)    # 3400 → 2200 Hz
    crush_mix = 0.20 + 0.45 * i        # 0.20 → 0.65
    tremolo_depth = 0.30 + 0.40 * i    # 0.30 → 0.70
    noise_vol = 0.06 + 0.40 * i        # 0.06 → 0.46
    return (
        f"[0:a]aresample=24000,"
        f"highpass=f={high_pass},lowpass=f={low_pass},"
        f"acrusher=bits=8:mode=log:mix={crush_mix:.2f},"
        f"tremolo=f=6.5:d={tremolo_depth:.2f},"
        f"volume=2.0[v];"
        f"[1:a]volume={noise_vol:.2f}[n];"
        f"[v][n]amix=inputs=2:duration=first:dropout_transition=0,"
        f"alimiter=limit=0.9[out]"
    )


def _ffmpeg_args(intensity: float) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", "pipe:0",
        "-f", "lavfi", "-i", "anoisesrc=c=pink:a=1.0",
        "-filter_complex", _build_filtergraph(intensity),
        "-map", "[out]",
        "-ac", "1",
        "-c:a", "libopus", "-b:a", "24k",
        "-f", "ogg", "pipe:1",
    ]


async def apply_radio_effect(
    audio: bytes, *, intensity: float = 0.5,
) -> bytes | None:
    """Return OGG/Opus bytes of ``audio`` with the radio effect applied.

    None if ffmpeg is unavailable, the input is empty, ffmpeg errors, or
    it doesn't finish within the timeout.
    """
    if not audio:
        return None
    if not ffmpeg_available():
        log.warning("apply_radio_effect: ffmpeg not on PATH.")
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            *_ffmpeg_args(intensity),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:  # pragma: no cover - spawn failure
        log.warning("apply_radio_effect: failed to spawn ffmpeg: %s", exc)
        return None
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=audio),
            timeout=_FFMPEG_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        log.warning("apply_radio_effect: ffmpeg timed out; killing.")
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None
    if proc.returncode != 0 or not out:
        tail = (err or b"").decode("utf-8", "replace")[-400:]
        log.warning(
            "apply_radio_effect: ffmpeg rc=%s, %d bytes out. stderr: %s",
            proc.returncode, len(out or b""), tail,
        )
        return None
    return out
