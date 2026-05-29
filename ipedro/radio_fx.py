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

    Built straight off the "far-away SSB" recipe, in order:
    EQ/bandpass → compression → light saturation → noise layer → pitch
    wobble/flutter → slapback delay → reverb → final EQ + volume dropouts.

    Inputs (the reverb IR is always last; the station is optional):
      0 = the voice,
      1 = a white-noise static bed,
      2 = the FM-swept heterodyne carrier whistle,
      3 = (optional) the bundled numbers-station recording,
      N = an exponential-decay noise impulse response for the reverb.

    Voice chain:
      1. ``highpass``×2 + ``lowpass``×2 → a hard SSB band (~450–2400 Hz);
         downsampled to 8 kHz; ``equalizer`` presence bump ~1.25 kHz for
         the nasal radio honk.
      2. ``acompressor`` 6:1 (+ ``dynaudnorm`` so the level is consistent
         regardless of how loud the source note was).
      3. light ``acrusher`` + ``asoftclip`` → grit/clip, not metal.
      4. (noise is mixed in as parallel beds, below.)
      5. ``vibrato`` ×2 → subtle pitch wobble + slow drift; ``tremolo`` ×4
         → detuned QSB fades that beat into smooth volume dropouts.
      6. ``aecho`` → a faint 180 ms slapback (far-away cue).
      7. ``afir`` convolution reverb against a dark, decaying IR, mixed in
         parallel and low (genuine reverb tail, not just echo).
      8. makeup ``volume`` (kept on the quiet/distant side) → final
         ``highpass``/``lowpass`` and a limiter after the mix.

    The beds: a breathing band-limited hiss with an opening squelch click,
    the faint swept whistle, and the numbers station drifting in/out on its
    own slow envelope — each on incommensurate cycles so nothing repeats in
    lockstep. Everything is mono. Higher intensity tightens the band a touch
    and shifts the sweep. ``amix=duration=first`` trims the beds to the voice.
    """
    i = max(0.0, min(1.0, intensity))
    high = int(420 + 60 * i)           # ~420 → 480 Hz  (SSB high-pass)
    low = int(2500 - 300 * i)          # ~2500 → 2200 Hz (SSB low-pass)
    noise_vol = 0.13 + 0.07 * i        # static bed — clearly audible
    het_vol = 0.10 + 0.05 * i          # heterodyne whistle (prominent)
    station_vol = 0.14 + 0.06 * i      # numbers-station layer (continuous)

    # The reverb IR is always the final input; its index shifts by whether
    # the optional station input is present (0 voice,1 noise,2 whistle,
    # [3 station], then IR).
    ir_idx = 4 if with_station else 3

    # 1 EQ/bandpass · 2 compress · 3 saturate · 5 pitch+fades · 6 slapback
    pre = (
        f"[0:a]aresample=8000,"
        f"highpass=f={high},highpass=f={high},lowpass=f={low},lowpass=f={low},"
        f"equalizer=f=1250:width_type=q:w=1.2:g=4.5,"
        f"acompressor=threshold=-20dB:ratio=6:attack=5:release=120:makeup=3,"
        f"dynaudnorm=p=0.9:m=18:g=15,"
        f"acrusher=bits=11:samples=1:mode=log:mix=0.25,volume=1.4,asoftclip=type=atan,"
        f"vibrato=f=0.30:d=0.08,vibrato=f=0.15:d=0.05,"
        f"tremolo=f=0.11:d=0.45,tremolo=f=0.17:d=0.40,"
        f"tremolo=f=0.27:d=0.30,tremolo=f=8:d=0.12,"
        f"aecho=0.9:0.92:180:0.18[pre]"
    )
    # 7 reverb: split → convolve the send with the dark decaying IR → mix
    # back low → makeup. (dry/wet kept low; reverb is a distance cue.)
    reverb = (
        f"[{ir_idx}:a]aresample=8000,volume=eval=frame:volume=exp(-4*t),"
        f"lowpass=f=2200,highpass=f=400[ir];"
        f"[pre]asplit=2[dry][snd];"
        f"[snd][ir]afir=dry=0:wet=1,volume=0.45[rv];"
        f"[dry][rv]amix=inputs=2:normalize=0,volume=2.6[v]"
    )
    # 4 noise beds (parallel): an audible breathing hiss that swells into
    # bursts (two incommensurate sines) + an opening squelch click; the
    # prominent swept whistle; and the numbers station on a single slow
    # swell with a level FLOOR so it stays present (never cuts to silence).
    noise = (
        f"[1:a]aresample=8000,highpass=f={high},lowpass=f={low},"
        f"volume=eval=frame:volume={noise_vol:.3f}"
        f"*(0.6+0.5*sin(2*PI*0.11*t))*(0.7+0.45*sin(2*PI*0.19*t))"
        f"+0.6*lt(t\\,0.05)[n]"
    )
    whistle = f"[2:a]volume={het_vol:.3f}[h]"

    parts = [pre, noise, whistle, reverb]
    mix_labels = "[v][n][h]"
    if with_station:
        parts.append(
            f"[3:a]aresample=8000,highpass=f={high},lowpass=f={low},"
            f"volume=eval=frame:volume={station_vol:.3f}*(0.6+0.4*sin(2*PI*0.045*t))[g]"
        )
        mix_labels = "[v][n][h][g]"
    n_inputs = 4 if with_station else 3
    # 8 final mix → EQ → limiter (mono is enforced by -ac 1 in the args).
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
    sweeps ±``dev`` Hz at 0.15 Hz, drifting on a smooth
    product-of-incommensurate-sines envelope. The envelope keeps a level
    FLOOR (≈0.55) so the whistle stays present and clearly audible while
    still swelling, rather than vanishing entirely. Deviation widens with
    intensity. Phase form ``carrier + (dev/rate)*sin(2π·rate·t)`` is FM.
    """
    i = max(0.0, min(1.0, intensity))
    dev = int(400 + 500 * i)           # 400 → 900 Hz sweep depth
    env = ("(0.55+0.45*(0.5+0.5*sin(2*PI*0.06*t))*(0.5+0.5*sin(2*PI*0.11*t)))")
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
    # Reverb impulse response: 2 s of white noise, decayed/darkened in the
    # filtergraph. Always the LAST input.
    args += ["-f", "lavfi", "-i", "anoisesrc=c=white:d=2"]
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
