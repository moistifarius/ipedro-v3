"""Radio-transmission audio effect for /ether voice broadcasts.

All DSP is pure Python (numpy + scipy.signal); ffmpeg is used only at the
edges to decode the input voice note → float32 PCM and to encode the
processed result → mono OGG/Opus. That keeps every effect sample-precise,
randomizable per render, and easy to reason about — which the filtergraph
approach was not.

Pipeline (in order, on the voice):

  1. Band-pass + presence boost           (Butterworth + narrow band sum)
  2. Compression                          (feed-forward envelope follower)
  3. Light tanh saturation
  4. Pitch wobble + slow drift            (per-sample resample)
  5. Constant −20 ¢ pitch shift           (the "SSB wrong-tuning" detune)
  6. Subtle ring modulation
  7. QSB tremolo                          (three detuned slow LFOs beating)
  8. Slapback delay (180 ms)              (far-away cue)
  9. Convolution reverb                   (dark exponential noise IR)

Then mixed in parallel:

  * Static bed — band-limited white noise that breathes on two LFOs and
    has sharp word-gap bursts.
  * Heterodyne whistle — FM-swept ~1 kHz tone with a high floor so it
    stays present.
  * HF squeals — sparse, fast linear chirps near 2 kHz.
  * Numbers station — the bundled "Swedish Rhapsody" recording, looped
    with a random entry point, on a high-floor envelope so it's a
    continuous bleed (range ≈ 0.50–1.00, never to zero).
  * Start AND end squelch clicks.

``intensity`` ∈ [0, 1] nudges band tightness and bed levels. If ffmpeg
isn't installed (``ffmpeg_available()`` is False) the caller should fall
back to a text broadcast.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import shutil
import subprocess
from pathlib import Path

import numpy as np
from scipy.signal import butter, fftconvolve, sosfiltfilt

log = logging.getLogger(__name__)

SR = 8000  # working sample rate (the audio bandwidth is < 3 kHz anyway)
_FFMPEG_TIMEOUT_SECONDS = 60

# Bundled numbers-station recording ("Swedish Rhapsody") used as one
# continuous-bleed interference layer. Optional: if the asset is missing,
# the effect still runs with just the synthetic beds.
_INTERFERENCE_FILE = Path(__file__).parent / "assets" / "swedish_rhapsody.ogg"


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _interference_path() -> Path | None:
    return _INTERFERENCE_FILE if _INTERFERENCE_FILE.is_file() else None


# ---------------------------------------------------------------- I/O
def _decode_to_pcm(audio: bytes) -> np.ndarray:
    """ffmpeg decode arbitrary input bytes → mono float32 at SR."""
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", "pipe:0",
            "-ac", "1", "-ar", str(SR),
            "-f", "f32le", "pipe:1",
        ],
        input=audio, capture_output=True, check=False,
        timeout=_FFMPEG_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0 or not proc.stdout:
        log.warning("decode failed rc=%s: %s",
                    proc.returncode, (proc.stderr or b"")[-400:].decode("utf-8", "replace"))
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def _decode_file_to_pcm(path: Path) -> np.ndarray:
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(path),
            "-ac", "1", "-ar", str(SR),
            "-f", "f32le", "pipe:1",
        ],
        capture_output=True, check=False, timeout=_FFMPEG_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0 or not proc.stdout:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def _encode_to_ogg(x: np.ndarray) -> bytes:
    """ffmpeg encode mono float32 PCM at SR → OGG/Opus voice-note bytes."""
    pcm = np.clip(x, -1.0, 1.0).astype(np.float32).tobytes()
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0",
            "-ac", "1", "-c:a", "libopus", "-b:a", "24k",
            "-f", "ogg", "pipe:1",
        ],
        input=pcm, capture_output=True, check=False,
        timeout=_FFMPEG_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0 or not proc.stdout:
        log.warning("encode failed rc=%s", proc.returncode)
        return b""
    return proc.stdout


# Cache the decoded numbers-station bed; it's ~120 s × 4 B = ~1 MB. Loaded
# lazily on first use so module import stays fast.
_STATION_PCM_CACHE: np.ndarray | None = None


def _station_pcm() -> np.ndarray:
    global _STATION_PCM_CACHE
    if _STATION_PCM_CACHE is not None:
        return _STATION_PCM_CACHE
    path = _interference_path()
    if path is None:
        _STATION_PCM_CACHE = np.zeros(0, dtype=np.float32)
        return _STATION_PCM_CACHE
    _STATION_PCM_CACHE = _decode_file_to_pcm(path)
    return _STATION_PCM_CACHE


# ---------------------------------------------------------------- DSP
def _bandpass(x: np.ndarray, lo: float, hi: float, order: int = 6) -> np.ndarray:
    """Zero-phase Butterworth band-pass."""
    sos = butter(order, [lo, hi], btype="band", fs=SR, output="sos")
    return sosfiltfilt(sos, x).astype(np.float32)


def _compress(
    x: np.ndarray, *, thr_db: float = -22, ratio: float = 6.0,
    attack_ms: float = 5, release_ms: float = 150, makeup_db: float = 4,
) -> np.ndarray:
    """Feed-forward envelope compressor (single-pole envelope follower)."""
    if len(x) == 0:
        return x
    a_a = math.exp(-1.0 / (SR * attack_ms / 1000))
    a_r = math.exp(-1.0 / (SR * release_ms / 1000))
    env = np.empty_like(x)
    env_i = 1e-10
    ax = np.abs(x)
    for n in range(len(x)):
        coef = a_a if ax[n] > env_i else a_r
        env_i = coef * env_i + (1 - coef) * ax[n]
        env[n] = env_i
    env_db = 20 * np.log10(env + 1e-10)
    over = np.maximum(0.0, env_db - thr_db)
    gr_db = -over * (1 - 1 / ratio) + makeup_db
    return (x * (10 ** (gr_db / 20))).astype(np.float32)


def _saturate(x: np.ndarray, drive: float = 1.5) -> np.ndarray:
    """tanh soft clip. Output is bounded in (-1, 1); quiet signals stay
    roughly linear, loud ones round off. No normalize-by-tanh(drive)
    because that lifts peaks above 1.0 for already-loud inputs."""
    return np.tanh(drive * x).astype(np.float32)


def _resample_by_rate(x: np.ndarray, rate: np.ndarray) -> np.ndarray:
    """Linear-interp time-varying resampler; ``rate[n]`` is the source-
    sample step per output sample. Used for both wobble and the constant
    detune (a pitch shift, not a time stretch — the output length matches
    cumulative rate, which is fine for our short broadcasts)."""
    if len(x) == 0:
        return x
    src_pos = np.cumsum(rate) - rate[0]
    src_pos = np.clip(src_pos, 0.0, len(x) - 1.001)
    i0 = src_pos.astype(np.int64)
    frac = (src_pos - i0).astype(np.float32)
    return ((1 - frac) * x[i0] + frac * x[i0 + 1]).astype(np.float32)


def _pitch_wobble(
    x: np.ndarray, *, base_cents: float = 0, wobble_cents: float = 15,
    rate_hz: float = 0.5, drift_cents: float = 10, drift_rate_hz: float = 0.1,
) -> np.ndarray:
    """Slow drift + faster wobble around a (possibly nonzero) base detune."""
    t = np.arange(len(x)) / SR
    cents = (base_cents
             + wobble_cents * np.sin(2 * np.pi * rate_hz * t)
             + drift_cents * np.sin(2 * np.pi * drift_rate_hz * t))
    rate = 2.0 ** (cents / 1200.0)  # cents → frequency/sample ratio
    return _resample_by_rate(x, rate)


def _ring_mod(x: np.ndarray, *, carrier_hz: float = 170, mix: float = 0.06) -> np.ndarray:
    """Subtle ring modulator — adds metallic SSB-style overtones."""
    t = np.arange(len(x)) / SR
    rm = x * np.sin(2 * np.pi * carrier_hz * t).astype(np.float32)
    return ((1 - mix) * x + mix * rm).astype(np.float32)


def _qsb_tremolo(x: np.ndarray) -> np.ndarray:
    """Three detuned slow LFOs beating into smooth deep amplitude fades
    (the "wandering intelligibility"), with a faster flutter on top."""
    t = np.arange(len(x)) / SR
    env = ((0.55 + 0.30 * np.sin(2 * np.pi * 0.11 * t))
           * (0.65 + 0.30 * np.sin(2 * np.pi * 0.17 * t))
           * (0.75 + 0.20 * np.sin(2 * np.pi * 0.27 * t))
           * (0.92 + 0.08 * np.sin(2 * np.pi * 8.0 * t)))
    return (x * env).astype(np.float32)


def _slapback(x: np.ndarray, *, delay_ms: float = 180, mix: float = 0.22) -> np.ndarray:
    d = int(delay_ms * SR / 1000)
    if d >= len(x):
        return x
    y = np.zeros_like(x)
    y[d:] = x[:-d] * mix
    return (x + y).astype(np.float32)


def _reverb(
    x: np.ndarray, *, decay_s: float = 1.6, wet: float = 0.30,
    dark_hz: float = 2000, rng: np.random.Generator | None = None,
) -> np.ndarray:
    """FFT convolution with a synthetic dark exponential-decay IR."""
    if len(x) == 0:
        return x
    r = rng if rng is not None else np.random.default_rng()
    n = int(decay_s * SR)
    noise = r.standard_normal(n).astype(np.float32)
    env = np.exp(-3.5 * np.linspace(0, 1, n)).astype(np.float32)
    ir = _bandpass(noise * env, 400, dark_hz, order=4)
    wet_sig = fftconvolve(x, ir, mode="same").astype(np.float32)
    # match wet energy to dry so ``wet`` is a meaningful mix knob
    dry_rms = float(np.sqrt(np.mean(x * x) + 1e-9))
    wet_rms = float(np.sqrt(np.mean(wet_sig * wet_sig) + 1e-9))
    if wet_rms > 0:
        wet_sig = wet_sig * (dry_rms / wet_rms)
    return (x + wet * wet_sig).astype(np.float32)


def _static_bed(
    n: int, *, level: float, burst_amp: float = 0.6,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Band-limited white noise that breathes on two LFOs and has sharp
    bursts at randomized positions (the kssht between words)."""
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    r = rng if rng is not None else np.random.default_rng()
    noise = r.standard_normal(n).astype(np.float32)
    noise = _bandpass(noise, 450, 2400, order=4)
    t = np.arange(n) / SR
    breathe = ((0.7 + 0.30 * np.sin(2 * np.pi * 0.13 * t))
               * (0.8 + 0.25 * np.sin(2 * np.pi * 0.21 * t)))
    burst = np.zeros(n, dtype=np.float32)
    n_bursts = max(2, n // (SR * 3))  # ~1 burst per 3 s
    for _ in range(n_bursts):
        width = int(SR * r.uniform(0.08, 0.22))
        center = int(r.integers(SR // 4, max(SR // 4 + 1, n - SR // 4)))
        amp = float(r.uniform(0.6, 1.0))
        s = max(0, center - width // 2)
        e = min(n, s + width)
        env = np.hanning(width).astype(np.float32)[: e - s] * amp
        burst[s:e] += env
    return (noise * (breathe + burst) * level).astype(np.float32)


def _whistle(n: int, *, level: float) -> np.ndarray:
    """FM-swept ~1 kHz heterodyne — the "wheeeooouup". Floor 0.60 so it's
    always audibly present while still swelling."""
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    t = np.arange(n) / SR
    dev, rate = 700.0, 0.15
    phase = 2 * np.pi * 1000 * t + (dev / rate) * np.sin(2 * np.pi * rate * t)
    env = (0.60 + 0.40 * (0.5 + 0.5 * np.sin(2 * np.pi * 0.06 * t))
           * (0.5 + 0.5 * np.sin(2 * np.pi * 0.11 * t)))
    return (np.sin(phase) * env * level).astype(np.float32)


def _squeals(
    n: int, *, level: float, rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Sparse fast HF chirps (~1.5–2.2 kHz) that come and go."""
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    r = rng if rng is not None else np.random.default_rng()
    out = np.zeros(n, dtype=np.float32)
    for _ in range(max(2, n // (SR * 4))):  # ~1 squeal per 4 s
        dur = int(SR * r.uniform(0.25, 0.9))
        start = int(r.integers(0, max(1, n - dur)))
        tt = np.arange(dur) / SR
        f0 = float(r.uniform(1500, 2200))
        f1 = f0 + float(r.uniform(-400, 400))
        phase = 2 * np.pi * (f0 * tt + (f1 - f0) * tt * tt / (2 * tt[-1] + 1e-9))
        env = np.hanning(dur).astype(np.float32)
        out[start:start + dur] += (np.sin(phase) * env * level).astype(np.float32)
    return out


def _station_layer(
    n: int, *, level: float, rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Numbers-station bleed. Loops the bundled asset from a random offset
    and rides a HIGH-FLOOR envelope (≈0.50–1.00) so it never cuts to
    silence — it's a continuous bleed, not an interrupted broadcast."""
    src = _station_pcm()
    if len(src) == 0 or n <= 0:
        return np.zeros(max(0, n), dtype=np.float32)
    r = rng if rng is not None else np.random.default_rng()
    offset = int(r.integers(0, len(src)))
    rolled = np.roll(src, -offset)
    reps = n // len(rolled) + 2
    tiled = np.tile(rolled, reps)[:n].astype(np.float32)
    tiled = _bandpass(tiled, 450, 2400, order=4)
    t = np.arange(n) / SR
    env = 0.75 + 0.25 * np.sin(2 * np.pi * 0.045 * t)  # range [0.50, 1.00]
    return (tiled * env * level).astype(np.float32)


def _clicks(
    n: int, *, level: float = 0.55,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Transmission squelch click at the START and END of the broadcast."""
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    r = rng if rng is not None else np.random.default_rng()
    out = np.zeros(n, dtype=np.float32)
    click_len = int(0.04 * SR)
    if click_len >= n:
        return out
    env = np.hanning(click_len).astype(np.float32)
    noise = r.standard_normal(click_len).astype(np.float32) * env
    noise = _bandpass(noise, 600, 3000, order=4)
    out[:click_len] = noise * level
    out[-click_len:] += noise * level * 0.85
    return out


# ---------------------------------------------------------------- pipeline
def _process_pcm(
    voice: np.ndarray, *, intensity: float,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Apply the full radio chain to a mono float32 PCM voice. Returns the
    mixed broadcast (mono, float32, at SR)."""
    if len(voice) == 0:
        return voice
    r = rng if rng is not None else np.random.default_rng()
    i = max(0.0, min(1.0, intensity))
    hp = 420 + 60 * i
    lp = 2500 - 300 * i

    x = voice.astype(np.float32, copy=False)
    # 1 bandpass + ~1.2 kHz presence boost (sum of a narrower band)
    x = _bandpass(x, hp, lp, order=6)
    x = x + _bandpass(x, 1000, 1500, order=2) * 0.6
    # 2 compress / level
    x = _compress(x, thr_db=-22, ratio=6.0, makeup_db=4.0)
    # 3 light saturation
    x = _saturate(x, drive=1.5)
    # 4 pitch wobble + slow drift
    x = _pitch_wobble(x, base_cents=0, wobble_cents=15, rate_hz=0.5,
                      drift_cents=10, drift_rate_hz=0.1)
    # 5 constant −20 ¢ detune (SSB "wrong tuning")
    x = _pitch_wobble(x, base_cents=-20, wobble_cents=0, rate_hz=1.0,
                      drift_cents=0, drift_rate_hz=1.0)
    # 6 subtle ring modulation
    x = _ring_mod(x, carrier_hz=170, mix=0.06)
    # 7 QSB amplitude fade
    x = _qsb_tremolo(x)
    # 8 slapback delay
    x = _slapback(x, delay_ms=180, mix=0.22)
    # 9 convolution reverb
    x = _reverb(x, decay_s=1.6, wet=0.30, dark_hz=2000, rng=r)
    # voice makeup (sits on top of the bed)
    x = x * 2.2

    n = len(x)
    out = (x
           + _static_bed(n, level=0.20 + 0.10 * i, burst_amp=0.6, rng=r)
           + _whistle(n, level=0.14 + 0.06 * i)
           + _squeals(n, level=0.10 + 0.04 * i, rng=r)
           + _station_layer(n, level=0.30 + 0.05 * i, rng=r)
           + _clicks(n, level=0.55, rng=r))
    # Final hard band + soft brick
    out = _bandpass(out, hp, lp, order=6)
    return np.tanh(out * 0.9).astype(np.float32)


# ---------------------------------------------------------------- entry
async def apply_radio_effect(
    audio: bytes, *, intensity: float = 0.5,
) -> bytes | None:
    """Return OGG/Opus bytes of ``audio`` with the radio effect applied.

    None if ffmpeg is unavailable, the input is empty, decode/encode
    fails, or the DSP errors. Runs the CPU-bound numpy DSP in a thread so
    it doesn't block the event loop."""
    if not audio:
        return None
    if not ffmpeg_available():
        log.warning("apply_radio_effect: ffmpeg not on PATH.")
        return None
    try:
        out = await asyncio.to_thread(_render_sync, audio, intensity)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("apply_radio_effect failed: %s", exc)
        return None
    return out or None


def _render_sync(audio: bytes, intensity: float) -> bytes:
    voice = _decode_to_pcm(audio)
    if len(voice) == 0:
        return b""
    rng = np.random.default_rng(random.randint(0, 2**31 - 1))
    out = _process_pcm(voice, intensity=intensity, rng=rng)
    return _encode_to_ogg(out)
