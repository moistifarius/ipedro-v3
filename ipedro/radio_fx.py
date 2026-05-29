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

    Models a long-haul HF/SSB transmission — the "2000 miles of ionospheric
    bounce" sound. Three inputs are mixed:
      0 = the voice, 1 = a white-noise band (static hiss),
      2 = a 1 kHz sine (the drifting heterodyne carrier whistle).

    The voice chain, in order:
      * downsample to 8 kHz + cascaded high/low pass → brutally narrow SSB
        passband (everything outside ~400–2600 Hz is gone);
      * two ``vibrato`` stages → the wandering pitch: a slow oscillator/
        ionospheric drift plus a faster auroral "warble";
      * ``acrusher`` (bit + sample crush) and ``asoftclip`` overdrive →
        the gritty, saturated comms timbre;
      * two ``tremolo`` stages → slow deep QSB fading plus fast flutter.

    The mix then gets ``compand`` (AGC pumping — hiss swells between words),
    a final pass of the bandpass, and a limiter. Higher intensity tightens
    the band, deepens every modulation, and raises the static + whistle.
    ``amix=duration=first`` trims the infinite noise/sine to the voice.
    """
    i = max(0.0, min(1.0, intensity))
    high = int(380 + 170 * i)          # 380 → 550 Hz  (narrow SSB)
    low = int(2600 - 900 * i)          # 2600 → 1700 Hz
    bits = round(8 - 4 * i)            # 8 → 4 bit crush
    samp = 1 + round(7 * i)            # 1 → 8 sample-hold crush
    crush_mix = 0.45 + 0.45 * i        # 0.45 → 0.90
    drive = 1.6 + 3.2 * i              # pre-clip gain → harder overdrive
    drift_d = 0.10 + 0.40 * i          # slow pitch wander depth
    warble_f = 5.0 + 5.0 * i           # auroral flutter rate (Hz)
    warble_d = 0.06 + 0.16 * i         # auroral flutter depth
    qsb_f = 0.15 + 0.55 * i            # slow fade rate (Hz)
    qsb_d = 0.45 + 0.45 * i            # slow fade depth
    flutter_f = 7.0 + 6.0 * i          # amplitude flutter rate (Hz)
    flutter_d = 0.15 + 0.30 * i        # amplitude flutter depth
    noise_vol = 0.20 + 0.55 * i        # static hiss level
    het_vol = 0.04 + 0.16 * i          # heterodyne whistle level
    return (
        # --- voice: band-limit → pitch drift/warble → crush/clip → fade ---
        f"[0:a]aresample=8000,"
        f"highpass=f={high},highpass=f={high},lowpass=f={low},lowpass=f={low},"
        f"vibrato=f=0.25:d={drift_d:.2f},vibrato=f={warble_f:.2f}:d={warble_d:.2f},"
        f"acrusher=bits={bits}:samples={samp}:mode=log:mix={crush_mix:.2f},"
        f"volume={drive:.2f},asoftclip=type=atan,"
        f"tremolo=f={qsb_f:.2f}:d={qsb_d:.2f},"
        f"tremolo=f={flutter_f:.2f}:d={flutter_d:.2f}[v];"
        # --- static: white hiss, band-limited to the same SSB window ---
        f"[1:a]aresample=8000,highpass=f={high},lowpass=f={low},"
        f"volume={noise_vol:.2f}[n];"
        # --- heterodyne: a slowly drifting, fading carrier whistle ---
        f"[2:a]vibrato=f=0.2:d=0.6,volume={het_vol:.2f},"
        f"tremolo=f={qsb_f:.2f}:d=0.7[h];"
        # --- mix → AGC pump → final band → limit → 48k for opus ---
        f"[v][n][h]amix=inputs=3:duration=first:dropout_transition=0:normalize=0,"
        f"compand=attacks=0.02:decays=0.3:"
        f"points=-80/-30|-45/-18|-20/-9|0/-5:soft-knee=6:gain=4,"
        f"highpass=f={high},lowpass=f={low},"
        f"alimiter=limit=0.95,aresample=48000[out]"
    )


def _ffmpeg_args(intensity: float) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", "pipe:0",
        "-f", "lavfi", "-i", "anoisesrc=c=white:a=1.0",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=8000",
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
