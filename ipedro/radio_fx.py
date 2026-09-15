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

  * Static bed — three-tier priority: a live WebSDR cache when available,
    a bundled ``shortwave_*.ogg`` if present, else a synthetic pink-noise
    bed with chaotic density variation, lightning crackles, and faint
    drifting carrier ghosts.
  * Heterodyne whistle — FM-swept ~1 kHz tone that genuinely fades in
    and out.
  * HF squeals — sparse, fast linear chirps near 2 kHz.
  * Start AND end squelch clicks.

``intensity`` ∈ [0, 1] nudges band tightness and bed levels. If ffmpeg
isn't installed (``ffmpeg_available()`` is False) the caller should fall
back to a text broadcast.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
from scipy.signal import butter, fftconvolve, sosfiltfilt

log = logging.getLogger(__name__)

SR = 8000  # working sample rate (the audio bandwidth is < 3 kHz anyway)
_FFMPEG_TIMEOUT_SECONDS = 60

# ---- Live shortwave fetch ---------------------------------------------------
# Static bed priority chain: live cached recording → bundled
# ``shortwave_*.ogg`` → synthetic (pink noise + crackles + carrier ghosts).
#
# Fetch is **lazy** — done on the /ether broadcast path, not on a background
# poll. ``/ether`` is a rare command, so a sleeping bot has no business
# hammering some volunteer's WebSDR. First call after a restart pays the
# fetch cost (~10–25 s, runs in a thread); the cached PCM is then reused
# for ~6 hours before the next call triggers a refresh.
#
# The source is a failover list of WebSDR / KiwiSDR / Icecast SSB stream
# URLs. ``_ensure_live_pcm()`` walks them in order until one yields valid
# PCM. Every layer fails open: no URL set → off, all URLs down → bundled
# or synthetic.
_LIVE_FETCH_DURATION_SECONDS = 30
_LIVE_FETCH_TIMEOUT_SECONDS = 25
_LIVE_CACHE_TTL_SECONDS = 6 * 3600  # 6h between refreshes
# No defaults: live fetch is OFF unless the operator explicitly sets
# RADIO_FX_LIVE_URLS in the environment. Reason: there is no reliable
# always-on public HTTP audio URL for SSB shortwave. Most WebSDRs
# (Twente, Northern Utah, KiwiSDRs) use WebSocket players or Java
# applets, not plain HTTP streams ffmpeg can pull from. To enable live
# fetch, point this at:
#   * an Icecast/MP3 stream of an SDR receiver you control or know works,
#   * a relay you operate, or
#   * any other URL whose body is an audio container ffmpeg can decode.
# When unset/empty, the bed falls through to bundled shortwave_*.ogg
# files in ipedro/assets/, then to the synthetic bed.
_DEFAULT_LIVE_URLS: tuple[str, ...] = ()


def _live_urls() -> tuple[str, ...]:
    """Configured failover list. Reads RADIO_FX_LIVE_URLS (comma-
    separated) if set; otherwise falls back to the built-in defaults.
    An explicit empty string disables live fetch entirely."""
    env = os.environ.get("RADIO_FX_LIVE_URLS")
    if env is None:
        return _DEFAULT_LIVE_URLS
    return tuple(p.strip() for p in env.split(",") if p.strip())


# In-memory cache: (pcm, fetched_at_monotonic, source_url) or None.
_live_cache: tuple[np.ndarray, float, str] | None = None


def _fetch_live_from_url(url: str) -> np.ndarray:
    """Pull ~30 s from one streaming URL, decoded to mono float32 at SR.

    Dispatches on URL scheme:
      * ``kiwi://host:port?freq=...&mode=...`` → KiwiSDR WebSocket client
        (anonymous connect, tune, 4-bit IMA-ADPCM decode, resample).
      * everything else → ffmpeg HTTP(S) fetch (Icecast / direct MP3 etc).

    Returns an empty array on any failure (timeout, bad status, decode
    error, empty body, malformed URL). Doesn't raise."""
    if url.startswith("kiwi://"):
        from ipedro import kiwisdr
        # KiwiSDR streams at 12 kHz int16 → convert to float32 at SR.
        int16_pcm = kiwisdr.fetch_pcm_from_url(
            url, duration_s=_LIVE_FETCH_DURATION_SECONDS,
            timeout=_LIVE_FETCH_TIMEOUT_SECONDS,
        )
        if int16_pcm.size < kiwisdr.KIWI_SAMPLE_RATE:  # need ≥ 1s of audio
            return np.zeros(0, dtype=np.float32)
        floats = int16_pcm.astype(np.float32) / 32768.0
        # 12000 → 8000 via 2/3 resampling.
        from scipy.signal import resample_poly
        out = resample_poly(floats, up=2, down=3).astype(np.float32)
        if out.size < SR or float(np.max(np.abs(out))) < 1e-4:
            return np.zeros(0, dtype=np.float32)
        return out
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-rw_timeout", str(_LIVE_FETCH_TIMEOUT_SECONDS * 1_000_000),
                "-i", url,
                "-t", str(_LIVE_FETCH_DURATION_SECONDS),
                "-vn", "-ac", "1", "-ar", str(SR),
                "-f", "f32le", "pipe:1",
            ],
            capture_output=True, check=False,
            timeout=_LIVE_FETCH_TIMEOUT_SECONDS + 5,
        )
    except subprocess.TimeoutExpired:
        log.info("live shortwave fetch %s timed out", url)
        return np.zeros(0, dtype=np.float32)
    if proc.returncode != 0 or not proc.stdout:
        tail = (proc.stderr or b"")[-200:].decode("utf-8", "replace")
        log.info("live shortwave fetch %s failed rc=%s: %s",
                 url, proc.returncode, tail)
        return np.zeros(0, dtype=np.float32)
    pcm = np.frombuffer(proc.stdout, dtype=np.float32)
    if pcm.size < SR or float(np.max(np.abs(pcm))) < 1e-4:
        return np.zeros(0, dtype=np.float32)
    return pcm.copy()


def _ensure_live_pcm() -> tuple[np.ndarray, str] | None:
    """Return (pcm, source_url) using the cache when fresh; otherwise
    walk the failover list, cache the first success, and return it.
    ``None`` if live fetch is disabled or every URL failed."""
    global _live_cache
    urls = _live_urls()
    if not urls:
        return None
    now = time.monotonic()
    if _live_cache is not None:
        pcm, fetched_at, src = _live_cache
        if now - fetched_at < _LIVE_CACHE_TTL_SECONDS and pcm.size > 0:
            return pcm, src
    for url in urls:
        log.info("live shortwave: fetching from %s", url)
        pcm = _fetch_live_from_url(url)
        if pcm.size > 0:
            _live_cache = (pcm, now, url)
            log.info("live shortwave: cached %.1fs from %s",
                     pcm.size / SR, url)
            return pcm, url
    log.info("live shortwave: all %d URLs failed, falling back", len(urls))
    return None


def _live_shortwave_bed(
    n: int, *, level: float, rng: np.random.Generator | None = None,
) -> np.ndarray | None:
    """Use the live-fetched cached recording as the static bed when
    available. Returns ``None`` on cold-miss or hard failure so the
    caller falls back through the priority chain."""
    if n <= 0:
        return None
    cached = _ensure_live_pcm()
    if cached is None:
        return None
    src, _ = cached
    if src.size == 0:
        return None
    r = rng if rng is not None else np.random.default_rng()
    offset = int(r.integers(0, src.size))
    rolled = np.roll(src, -offset)
    tiled = np.tile(rolled, n // rolled.size + 2)[:n].astype(np.float32)
    # Band-limit into the SSB window so it sits where the other beds do.
    tiled = _bandpass(tiled, 420, 2400, order=4)
    return (tiled * level).astype(np.float32)


def reset_live_cache() -> None:
    """Test/admin helper: drop the cached live PCM so the next call
    fetches again."""
    global _live_cache
    _live_cache = None


# Which bed the last render actually used. ``None`` until something is
# broadcast. Exposed via ``last_bed_source()`` so the admin can confirm
# whether the live fetch is actually being applied or it's falling back.
_last_bed_source: str | None = None


def _record_last_bed_source(name: str) -> None:
    global _last_bed_source
    _last_bed_source = name


def last_bed_source() -> str | None:
    """Return the bed source used by the most recent radio render —
    one of ``"live"``, ``"bundled"``, ``"synthetic"``, or ``None`` if
    nothing has been rendered yet."""
    return _last_bed_source


def live_cache_status() -> dict[str, object]:
    """Snapshot of the live-fetch state for an admin status command.

    Returns a dict with:
      * ``urls``: tuple of configured failover URLs (empty when disabled).
      * ``cached``: True if a PCM chunk is currently cached.
      * ``cached_source``: which URL the cache came from (or None).
      * ``cached_seconds``: duration of the cached audio (0 if no cache).
      * ``cached_age_seconds``: seconds since the cache was filled
        (None if no cache).
      * ``ttl_seconds``: how long a cache entry stays valid before the
        next call triggers a refresh.
      * ``bundled_count``: number of bundled ``shortwave_*.ogg`` files.
      * ``last_bed_source``: bed source used by the most recent render.
    """
    cached_seconds = 0.0
    cached_age = None
    cached_source = None
    cached = False
    if _live_cache is not None:
        pcm, fetched_at, src = _live_cache
        cached = pcm.size > 0
        cached_seconds = pcm.size / SR
        cached_age = time.monotonic() - fetched_at
        cached_source = src
    return {
        "urls": _live_urls(),
        "cached": cached,
        "cached_source": cached_source,
        "cached_seconds": cached_seconds,
        "cached_age_seconds": cached_age,
        "ttl_seconds": _LIVE_CACHE_TTL_SECONDS,
        "bundled_count": len(_shortwave_pool()),
        "last_bed_source": _last_bed_source,
    }



def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


# Bundled shortwave/SSB recordings used as the static bed. Drop one (or
# more) ``shortwave_*.ogg`` files into ``ipedro/assets/`` and they'll
# auto-replace the synthetic noise bed at runtime — a random one is
# picked per broadcast. If none exist, the synthetic shortwave bed
# (pink noise + crackles + carrier ghosts) is used instead.
_SHORTWAVE_DIR = Path(__file__).parent / "assets"


def _shortwave_pool() -> list[Path]:
    """List of bundled shortwave recordings, sorted for determinism."""
    if not _SHORTWAVE_DIR.is_dir():
        return []
    return sorted(_SHORTWAVE_DIR.glob("shortwave_*.ogg"))


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


def _pink_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    """Cheap pink-ish noise via a one-pole low-pass on white noise.
    Output is RMS-normalised so it slots in like the old white bed."""
    w = rng.standard_normal(n).astype(np.float32)
    # y[k] = a y[k-1] + (1-a) x[k]; a≈0.98 gives ~ -3 dB/oct (pink-ish)
    out = np.empty_like(w)
    y = 0.0
    a = 0.98
    one_minus_a = 1.0 - a
    for i in range(n):
        y = a * y + one_minus_a * w[i]
        out[i] = y
    sd = float(np.std(out)) + 1e-9
    return (out / sd).astype(np.float32)


def _static_bed(
    n: int, *, level: float, burst_amp: float = 0.6,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Synthetic shortwave bed when no real recording is bundled.

    Closer to actual SSB hash than a plain white bandpass: pink-band
    noise (1/f-ish, more low-mid energy) modulated by *three* slow LFOs
    at incommensurate rates so density wanders chaotically, plus brief
    LIGHTNING-style crackles (sub-10 ms peaks at random offsets) and a
    handful of faint, drifting CARRIER GHOSTS (weak SSB sines bleeding
    in from other "stations"). The old kssht word-gap bursts are folded
    into the density variation.
    """
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    r = rng if rng is not None else np.random.default_rng()
    bed = _bandpass(_pink_noise(n, r), 380, 2500, order=4)
    t = np.arange(n) / SR
    # Three irregular sub-Hz LFOs → chaotic density envelope.
    density = ((0.5 + 0.5 * np.sin(2 * np.pi * 0.07 * t + 1.3))
               * (0.6 + 0.4 * np.sin(2 * np.pi * 0.13 * t + 2.7))
               * (0.7 + 0.3 * np.sin(2 * np.pi * 0.23 * t + 0.6))) * 1.6 + 0.4
    bed = bed * density.astype(np.float32)
    # Lightning crackles — short sharp spikes
    n_crackles = max(4, n // (SR * 2))  # ~1 every 2 s
    for _ in range(n_crackles):
        width = int(r.uniform(20, 80))  # 2.5–10 ms
        pos = int(r.integers(0, max(1, n - width)))
        peak = float(r.uniform(0.4, 1.4)) * burst_amp
        env = np.hanning(width).astype(np.float32) ** 2
        crack = (r.standard_normal(width).astype(np.float32) * env * peak)
        bed[pos:pos + width] += crack
    # Faint carrier ghosts — weak drifting sines from other stations
    n_ghosts = max(2, n // (SR * 5))
    for _ in range(n_ghosts):
        dur = int(r.uniform(0.4, 1.6) * SR)
        start = int(r.integers(0, max(1, n - dur)))
        tt = np.arange(dur) / SR
        f = float(r.uniform(700, 2200))
        drift = float(r.uniform(-40, 40))
        env = np.hanning(dur).astype(np.float32) * float(r.uniform(0.06, 0.18))
        phase = 2 * np.pi * (f * tt + drift * tt * tt / 2)
        bed[start:start + dur] += (np.sin(phase) * env).astype(np.float32)
    # Re-apply the SSB band so crackles don't leak above the roof
    bed = _bandpass(bed, 420, 2400, order=6)
    return (bed * level).astype(np.float32)


def _shortwave_real_bed(
    n: int, *, level: float, rng: np.random.Generator | None = None,
) -> np.ndarray | None:
    """Use a bundled real shortwave recording as the static bed.

    Returns None when no ``shortwave_*.ogg`` is present in the assets
    directory (caller falls back to the synthetic bed). Picks a random
    file from the pool, then a random entry point within that file, so
    no two transmissions catch the same slice."""
    pool = _shortwave_pool()
    if not pool or n <= 0:
        return None
    r = rng if rng is not None else np.random.default_rng()
    path = pool[int(r.integers(0, len(pool)))]
    src = _decode_file_to_pcm(path)
    if len(src) == 0:
        return None
    offset = int(r.integers(0, len(src)))
    rolled = np.roll(src, -offset)
    tiled = np.tile(rolled, n // len(rolled) + 2)[:n].astype(np.float32)
    # Band-limit to the SSB window so it sits where the rest of the bed does.
    tiled = _bandpass(tiled, 420, 2400, order=4)
    return (tiled * level).astype(np.float32)


def _whistle(n: int, *, level: float) -> np.ndarray:
    """FM-swept ~1 kHz heterodyne — the "wheeeooouup".

    Envelope range is [0.10, 1.00] so the whistle genuinely fades to
    near-silent between sweeps, rather than sitting in the mix at a
    high floor. Two incommensurate sines drive the swell so it never
    pulses on a metronomic cycle.
    """
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    t = np.arange(n) / SR
    dev, rate = 700.0, 0.15
    phase = 2 * np.pi * 1000 * t + (dev / rate) * np.sin(2 * np.pi * rate * t)
    env = (0.10 + 0.90
           * (0.5 + 0.5 * np.sin(2 * np.pi * 0.06 * t))
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
    # Bed priority: live-fetched WebSDR cache → bundled shortwave_*.ogg
    # → synthetic (pink + crackles + ghosts). Each layer fails open.
    bed = _live_shortwave_bed(n, level=0.40 + 0.10 * i, rng=r)
    bed_source = "live"
    if bed is None:
        bed = _shortwave_real_bed(n, level=0.45 + 0.10 * i, rng=r)
        bed_source = "bundled"
    if bed is None:
        bed = _static_bed(n, level=0.22 + 0.08 * i, burst_amp=0.6, rng=r)
        bed_source = "synthetic"
    _record_last_bed_source(bed_source)
    out = (x
           + bed
           + _whistle(n, level=0.09 + 0.04 * i)
           + _squeals(n, level=0.10 + 0.04 * i, rng=r)
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
