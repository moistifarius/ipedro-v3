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

    The voice chain, in order (loosely the classic "fighting the medium"
    recipe — bandpass, compress, distort, modulate, delay, reverb):
      * downsample to 8 kHz + cascaded high/low pass → brutally narrow SSB
        passband (everything outside ~400–2600 Hz is gone);
      * ``acompressor`` → the flattened, squashed comms dynamic;
      * two ``vibrato`` stages → the wandering pitch: a slow oscillator/
        ionospheric drift plus a faster auroral "warble";
      * ``acrusher`` (bit + sample crush) and ``asoftclip`` overdrive →
        the gritty, saturated comms timbre;
      * three ``tremolo`` stages → two *detuned* slow-and-deep QSB LFOs
        that beat against each other so the signal wanders between
        almost-readable and lost-under-the-noise, plus a fast flutter;
      * a time-varying ``volume`` gate → DROPOUTS: the voice briefly
        vanishes (to ~8%) when two slow LFOs coincide — a weak signal
        doesn't just get noisy, it disappears;
      * two ``aecho`` stages → a slapback plus a faint dark reverb tail,
        for the "far away / swallowed by space" distance.

    The static is band-limited white noise that *punches up in bursts*
    (another time-varying ``volume``), so when the voice dips or drops the
    transmission is momentarily buried in garbage. A drifting FM-swept
    heterodyne whistle sits behind it all. A light ``compand`` keeps some
    AGC hiss-pumping without flattening the QSB. Everything is mono.
    Higher intensity tightens the band and deepens every modulation.
    ``amix=duration=first`` trims the infinite noise/whistle to the voice.
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
    # Two detuned, deep, slow QSB LFOs. Their beat makes intelligibility
    # wander instead of pulsing metronomically.
    qsb_a_d = 0.55 + 0.35 * i          # 0.55 → 0.90 deep
    qsb_b_d = 0.45 + 0.35 * i          # 0.45 → 0.80 deep
    flutter_f = 7.0 + 6.0 * i          # amplitude flutter rate (Hz)
    flutter_d = 0.15 + 0.30 * i        # amplitude flutter depth
    noise_vol = 0.20 + 0.40 * i        # baseline static hiss level
    burst_amp = 1.5 + 1.0 * i          # how hard the static bursts punch up
    het_vol = 0.14 + 0.16 * i          # heterodyne whistle level (toned down)
    return (
        # --- voice: band-limit → compress → pitch drift/warble → crush/clip
        #     → QSB fade → DROPOUTS → slapback delay → faint reverb tail ---
        f"[0:a]aresample=8000,"
        f"highpass=f={high},highpass=f={high},lowpass=f={low},lowpass=f={low},"
        f"acompressor=threshold=-20dB:ratio=6:attack=5:release=120:makeup=3,"
        f"vibrato=f=0.25:d={drift_d:.2f},vibrato=f={warble_f:.2f}:d={warble_d:.2f},"
        f"acrusher=bits={bits}:samples={samp}:mode=log:mix={crush_mix:.2f},"
        f"volume={drive:.2f},asoftclip=type=atan,"
        f"tremolo=f=0.13:d={qsb_a_d:.2f},tremolo=f=0.21:d={qsb_b_d:.2f},"
        f"tremolo=f={flutter_f:.2f}:d={flutter_d:.2f},"
        # signal cuts: voice briefly drops to ~8% when two slow LFOs coincide
        f"volume=eval=frame:volume=1-0.92*gt(sin(2*PI*0.23*t)*sin(2*PI*0.37*t)\\,0.5),"
        f"aecho=0.9:0.85:220:0.18,"               # slapback (far away)
        f"aecho=0.85:0.55:70|150|260:0.3|0.2|0.12"  # faint dark reverb tail
        f"[v];"
        # --- static: band-limited white hiss that PUNCHES UP in bursts ---
        f"[1:a]aresample=8000,highpass=f={high},lowpass=f={low},"
        f"volume=eval=frame:volume={noise_vol:.2f}*"
        f"(1+{burst_amp:.2f}*gt(sin(2*PI*0.29*t)*sin(2*PI*0.41*t)\\,0.5))[n];"
        # --- heterodyne: FM-swept whistle (built in the source), leveled ---
        f"[2:a]volume={het_vol:.2f}[h];"
        # --- mix → light AGC → final band → limit → 48k for opus ---
        f"[v][n][h]amix=inputs=3:duration=first:dropout_transition=0:normalize=0,"
        f"compand=attacks=0.05:decays=0.6:"
        f"points=-70/-40|-40/-25|-15/-12|0/-6:soft-knee=8:gain=2,"
        f"highpass=f={high},lowpass=f={low},"
        f"alimiter=limit=0.95,aresample=48000[out]"
    )


def _heterodyne_lavfi(intensity: float) -> str:
    """lavfi source string for the drifting tuning whistle.

    A pure FM-swept sine (the "wheeeooouup"): a 1 kHz carrier whose pitch
    sweeps ±``dev`` Hz at 0.15 Hz, gated in and out by a squared 0.22 Hz
    envelope so it surfaces and vanishes. Deviation widens with intensity.
    Phase form ``carrier + (dev/rate)*sin(2π·rate·t)`` gives true FM.
    """
    i = max(0.0, min(1.0, intensity))
    dev = int(400 + 500 * i)           # 400 → 900 Hz sweep depth
    env = "(0.5+0.5*sin(2*PI*0.22*t))*(0.5+0.5*sin(2*PI*0.22*t))"
    tone = f"sin(2*PI*1000*t+({dev}/0.15)*sin(2*PI*0.15*t))"
    return f"aevalsrc={env}*{tone}:s=8000"


def _ffmpeg_args(intensity: float) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", "pipe:0",
        "-f", "lavfi", "-i", "anoisesrc=c=white:a=1.0",
        "-f", "lavfi", "-i", _heterodyne_lavfi(intensity),
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
