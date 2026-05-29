"""Radio-transmission audio effect for /ether voice broadcasts.

Takes arbitrary input audio (a Telegram voice note's OGG/Opus, or TTS
output) and runs it through an ffmpeg filtergraph that makes it sound
like a faint, far-away HF/SSB radio signal: a tight bandpass, drifting
pitch, smooth ionospheric (QSB) fading, a static bed, a swept heterodyne
whistle, and an optional numbers-station bleed. The voice rides on top
and stays intelligible; the interference layers each drift on their own
slow cycles so it never sounds like switched, repeating effects. Output
is mono OGG/Opus, ready for ``send_voice``.

Everything is done in a single ffmpeg invocation over pipes — no temp
files, no PCM round-trip in Python. ``intensity`` ∈ [0,1] scales the
degradation, so a transmission can be anything from "slightly distant"
to "barely punching through the noise".

If ffmpeg isn't installed (``ffmpeg_available()`` is False) the caller
should fall back to a text broadcast.
"""

from __future__ import annotations

import asyncio
import logging
import random
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_FFMPEG_TIMEOUT_SECONDS = 60

# Bundled numbers-station recording ("Swedish Rhapsody") used as one layer
# of the interference — a ghostly transmission that swells in and bleeds
# over the voice during the "eat" events. Optional: if the asset is
# missing, the effect still runs with just hiss + whistle.
_INTERFERENCE_FILE = Path(__file__).parent / "assets" / "swedish_rhapsody.ogg"
_INTERFERENCE_DURATION = 120.0  # seconds in the baked asset (for random seek)


def ffmpeg_available() -> bool:
    """True iff an ffmpeg binary is on PATH."""
    return shutil.which("ffmpeg") is not None


def _interference_path() -> Path | None:
    """Path to the bundled numbers-station bed, or None if not present."""
    return _INTERFERENCE_FILE if _INTERFERENCE_FILE.is_file() else None


def _build_filtergraph(intensity: float, *, with_station: bool = False) -> str:
    """Build the ffmpeg -filter_complex string for the given intensity.

    Models a long-haul HF/SSB transmission — the "2000 miles of ionospheric
    bounce" sound. The realism comes from *continuous* modulation rather
    than switched effects: every layer rides smooth sine envelopes at its
    own incommensurate rate, so they drift in and out of phase and the
    "swallowed" moments happen by coincidence, never on a shared on/off
    gate (hard gates sound mechanical). Inputs:
      0 = the voice, 1 = a white-noise band (the static bed),
      2 = the FM-swept heterodyne carrier whistle,
      3 = (optional) the bundled numbers-station recording.

    The voice chain aims for *thin and distant*, not low/gravelly:
      * downsample to 8 kHz + cascaded high/low pass → narrow SSB band
        (~440–2700 Hz; the low body is gone);
      * ``acompressor`` + ``dynaudnorm`` → squashed comms dynamic and a
        consistent level regardless of how loud the source note was;
      * an ``equalizer`` presence bump near 1.55 kHz → the tinny, nasal
        radio-speaker honk;
      * two ``vibrato`` stages → the wandering pitch (slow ionospheric
        drift + faster auroral warble);
      * a light ``acrusher`` (bit-only, no sample-hold) + ``asoftclip``
        overdrive → a subtle electrical edge, *not* the old gravel;
      * four ``tremolo`` stages → three *detuned* slow QSB fades that beat
        together into smooth, organic deep fades, plus a faster flutter.
        When the fade bottoms out the steady static naturally takes over —
        that's the voice being "eaten", no gating required;
      * two ``aecho`` stages (slapback + dark tail) → distance;
      * a makeup ``volume`` so the voice rides clearly on top.

    The static bed breathes on two incommensurate sines (+ an opening
    squelch click); the whistle and numbers station each drift in and out
    on their own slow product-of-sines envelopes. Higher intensity tightens
    the band and deepens every modulation. ``amix=duration=first`` trims
    the looping/infinite beds to the voice length.
    """
    i = max(0.0, min(1.0, intensity))
    high = int(440 + 120 * i)          # 440 → 560 Hz  (thin, no low body)
    low = int(2700 - 500 * i)          # 2700 → 2200 Hz
    bits = round(12 - 2 * i)           # 12 → 10  (subtle bit grit, no s-hold)
    crush_mix = 0.18 + 0.18 * i        # 0.18 → 0.36 (light)
    drive = 1.2 + 0.6 * i              # gentle overdrive edge (not gravel)
    drift_d = 0.06 + 0.16 * i          # slow pitch wander depth
    warble_f = 6.0 + 4.0 * i           # auroral flutter rate (Hz)
    warble_d = 0.04 + 0.08 * i         # auroral flutter depth
    # Three detuned QSB tremolos (≥0.1 Hz, ffmpeg's floor) that beat into
    # smooth organic fades, + a faster flutter.
    qa = 0.35 + 0.22 * i
    qb = 0.30 + 0.22 * i
    qc = 0.24 + 0.18 * i
    flutter_f = 7.0 + 4.0 * i
    flutter_d = 0.05 + 0.10 * i
    noise_vol = 0.030 + 0.040 * i      # static bed (breathes, ~steady)
    het_vol = 0.022 + 0.030 * i        # heterodyne whistle (faint)
    station_vol = 0.12 + 0.06 * i      # numbers-station layer (one of several)

    voice = (
        f"[0:a]aresample=8000,"
        f"highpass=f={high},highpass=f={high},lowpass=f={low},lowpass=f={low},"
        f"acompressor=threshold=-18dB:ratio=3.5:attack=12:release=180:makeup=2,"
        f"dynaudnorm=p=0.9:m=18:g=15,"
        f"equalizer=f=1550:width_type=q:w=1.1:g=3.5,"
        f"vibrato=f=0.22:d={drift_d:.2f},vibrato=f={warble_f:.2f}:d={warble_d:.2f},"
        f"acrusher=bits={bits}:samples=1:mode=log:mix={crush_mix:.2f},"
        f"volume={drive:.2f},asoftclip=type=atan,"
        f"tremolo=f=0.11:d={qa:.2f},tremolo=f=0.16:d={qb:.2f},"
        f"tremolo=f=0.23:d={qc:.2f},tremolo=f={flutter_f:.2f}:d={flutter_d:.2f},"
        f"aecho=1.0:0.9:200:0.16,aecho=0.9:0.85:130|260:0.20|0.12,volume=2.4[v]"
    )
    # Static: smooth breathing baseline (two incommensurate sines) + a short
    # opening squelch click. Roughly steady, so it takes over when the voice
    # fades — no surge gate needed.
    noise = (
        f"[1:a]aresample=8000,highpass=f={high},lowpass=f={low},"
        f"volume=eval=frame:volume={noise_vol:.3f}"
        f"*(0.55+0.35*sin(2*PI*0.11*t))*(0.7+0.3*sin(2*PI*0.17*t))"
        f"+0.35*lt(t\\,0.04)[n]"
    )
    # Whistle: faint, leveled (its drift/sweep lives in the source).
    whistle = f"[2:a]volume={het_vol:.3f}[h]"

    parts = [voice, noise, whistle]
    mix_labels = "[v][n][h]"
    if with_station:
        # Numbers station: drifts in and out on its own slow product-of-sines
        # envelope (mostly low, occasionally swelling up) — one layer of the
        # interference, never the whole bed.
        parts.append(
            f"[3:a]aresample=8000,highpass=f={high},lowpass=f={low},"
            f"volume=eval=frame:volume={station_vol:.3f}*2.0"
            f"*(0.5+0.5*sin(2*PI*0.037*t))*(0.5+0.5*sin(2*PI*0.061*t))[g]"
        )
        mix_labels = "[v][n][h][g]"
    n_inputs = 4 if with_station else 3
    mix = (
        f"{mix_labels}amix=inputs={n_inputs}:duration=first:"
        f"dropout_transition=0:normalize=0,"
        f"highpass=f={high},lowpass=f={low},"
        f"alimiter=limit=0.95,aresample=48000[out]"
    )
    return ";".join(parts) + ";" + mix


def _heterodyne_lavfi(intensity: float) -> str:
    """lavfi source string for the drifting tuning whistle.

    A pure FM-swept sine (the "wheeeooouup"): a 1 kHz carrier whose pitch
    sweeps ±``dev`` Hz at 0.15 Hz, drifting in and out on a smooth
    product-of-two-incommensurate-sines envelope so it surfaces and
    vanishes irregularly (not on a regular pulse). Deviation widens with
    intensity. Phase form ``carrier + (dev/rate)*sin(2π·rate·t)`` is FM.
    """
    i = max(0.0, min(1.0, intensity))
    dev = int(400 + 500 * i)           # 400 → 900 Hz sweep depth
    env = "(0.5+0.5*sin(2*PI*0.07*t))*(0.5+0.5*sin(2*PI*0.13*t))"
    tone = f"sin(2*PI*1000*t+({dev}/0.15)*sin(2*PI*0.15*t))"
    return f"aevalsrc={env}*{tone}:s=8000"


def _ffmpeg_args(intensity: float) -> list[str]:
    """Assemble the ffmpeg argv. Adds the numbers-station input only when
    the bundled asset is present; otherwise the effect runs with just the
    hiss + whistle layers."""
    station = _interference_path()
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", "pipe:0",
        "-f", "lavfi", "-i", "anoisesrc=c=white:a=1.0",
        "-f", "lavfi", "-i", _heterodyne_lavfi(intensity),
    ]
    if station is not None:
        # Loop the (finite) asset and enter at a random point so each
        # transmission catches a different slice. amix=duration=first
        # trims it back to the voice length.
        offset = random.uniform(0, _INTERFERENCE_DURATION)
        args += ["-stream_loop", "-1", "-ss", f"{offset:.2f}", "-i", str(station)]
    args += [
        "-filter_complex", _build_filtergraph(intensity, with_station=station is not None),
        "-map", "[out]",
        "-ac", "1",
        "-c:a", "libopus", "-b:a", "24k",
        "-f", "ogg", "pipe:1",
    ]
    return args


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
