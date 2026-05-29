"""Tests for the radio-transmission audio effect.

DSP is pure-Python (numpy + scipy.signal); ffmpeg is only used to decode
voice-note bytes → float32 PCM and to re-encode the result to OGG/Opus.
These tests exercise the in-process DSP blocks directly (no ffmpeg
needed), and the high-level dispatch path with ffmpeg stubbed out.
"""

from __future__ import annotations

import shutil

import numpy as np
import pytest

from ipedro import radio_fx


SR = radio_fx.SR

# The station decode needs ffmpeg on PATH. CI environments may not have
# it; production (Docker) does. Tests that actually pipe through ffmpeg
# are skipped without it.
needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg not on PATH (required to decode the numbers-station asset)",
)


def _impulse(n: int, pos: int = 0, amp: float = 1.0) -> np.ndarray:
    x = np.zeros(n, dtype=np.float32)
    x[pos] = amp
    return x


def _tone(n: int, hz: float, amp: float = 0.5) -> np.ndarray:
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


# ---------------------------------------------------------------- DSP blocks
def test_bandpass_attenuates_out_of_band_tones():
    # Test frequencies must stay below Nyquist (SR/2 = 4 kHz) to avoid
    # aliasing — a 6 kHz tone at 8 kHz SR folds to 2 kHz and is *in* band.
    n = SR * 2
    in_band = _tone(n, 1200)
    out_low = _tone(n, 100)
    out_high = _tone(n, 3700)
    y_in = radio_fx._bandpass(in_band, 450, 2400)
    y_lo = radio_fx._bandpass(out_low, 450, 2400)
    y_hi = radio_fx._bandpass(out_high, 450, 2400)
    rms = lambda v: float(np.sqrt(np.mean(v * v)))
    assert rms(y_lo) < rms(y_in) * 10 ** (-30 / 20)
    assert rms(y_hi) < rms(y_in) * 10 ** (-15 / 20)  # 6th order Butterworth slope
    assert rms(y_in) > rms(in_band) * 10 ** (-6 / 20)


def test_compressor_reduces_steady_state_level_of_loud_signal():
    # Compare RMS of the settled tail (skip the attack startup) — peaks at
    # t=0 haven't been reduced yet because the envelope follower hasn't
    # built up, so max-over-whole-signal isn't the right metric.
    n = SR * 2
    x = _tone(n, 1000, amp=0.9)
    y = radio_fx._compress(x, thr_db=-20, ratio=8.0, makeup_db=0)
    tail = lambda v: v[SR // 2:]
    rms = lambda v: float(np.sqrt(np.mean(v * v)))
    assert rms(tail(y)) < rms(tail(x)) * 0.5  # well over 6 dB of reduction


def test_saturate_is_bounded_and_passes_quiet_signals():
    quiet = _tone(SR, 1000, amp=0.05)
    loud = _tone(SR, 1000, amp=10.0)
    y_q = radio_fx._saturate(quiet, drive=1.5)
    y_l = radio_fx._saturate(loud, drive=1.5)
    # tanh output is bounded in (-1, 1).
    assert float(np.max(np.abs(y_l))) <= 1.0 + 1e-6
    # Quiet input is roughly linear (tanh(1.5 * x) ≈ 1.5 * x for tiny x).
    assert np.allclose(y_q, 1.5 * quiet, atol=0.005)


def test_pitch_wobble_returns_finite_audio_of_similar_length():
    n = SR * 3
    x = _tone(n, 1000, amp=0.5)
    y = radio_fx._pitch_wobble(x, base_cents=-20, wobble_cents=15,
                               rate_hz=0.5, drift_cents=10, drift_rate_hz=0.1)
    assert y.dtype == np.float32
    assert np.all(np.isfinite(y))
    # Down-pitched output is shorter than the input but not by much.
    assert 0.95 * n <= len(y) <= n


def test_ring_mod_introduces_sidebands():
    n = SR
    x = _tone(n, 1000, amp=0.5)
    y = radio_fx._ring_mod(x, carrier_hz=170, mix=0.5)
    fx = np.abs(np.fft.rfft(x))
    fy = np.abs(np.fft.rfft(y))
    freqs = np.fft.rfftfreq(n, 1 / SR)
    # Sidebands appear at 1000 ± 170 Hz.
    def bin_energy(target):
        idx = int(np.argmin(np.abs(freqs - target)))
        return float(fy[max(0, idx - 1): idx + 2].sum())
    assert bin_energy(830) > bin_energy(900) * 5
    assert bin_energy(1170) > bin_energy(1100) * 5
    # Dry tone has no such sidebands.
    fx_side = float(fx[int(np.argmin(np.abs(freqs - 830)))])
    assert fx_side < bin_energy(830) * 0.1


def test_slapback_adds_a_delayed_copy():
    n = SR
    x = _impulse(n, pos=0, amp=1.0)
    y = radio_fx._slapback(x, delay_ms=180, mix=0.5)
    d = int(0.18 * SR)
    assert y[0] == pytest.approx(1.0)
    assert y[d] == pytest.approx(0.5, rel=1e-3)


def test_reverb_adds_tail_energy_after_input_decay():
    n = SR * 2
    x = np.zeros(n, dtype=np.float32)
    x[: SR // 10] = 0.5  # 100 ms burst at the start
    y = radio_fx._reverb(x, decay_s=1.0, wet=0.6, dark_hz=2000,
                         rng=np.random.default_rng(0))
    tail = y[SR // 5:]
    assert float(np.sqrt(np.mean(tail * tail))) > 1e-3


def test_static_bed_is_audible_and_band_limited():
    n = SR * 3
    bed = radio_fx._static_bed(n, level=0.20, rng=np.random.default_rng(0))
    rms = float(np.sqrt(np.mean(bed * bed)))
    assert rms > 0.005
    # Energy above 3 kHz should be small relative to in-band energy.
    f = np.fft.rfftfreq(n, 1 / SR)
    spec = np.abs(np.fft.rfft(bed))
    in_band = float(spec[(f > 500) & (f < 2300)].sum())
    out_band = float(spec[f > 3000].sum())
    assert out_band < in_band * 0.05


def test_whistle_has_high_floor_envelope():
    """The whistle envelope floor keeps it audibly present at all times."""
    n = SR * 6
    w = radio_fx._whistle(n, level=0.20)
    abs_w = np.abs(w)
    # The minimum envelope (over a small window) is well above zero.
    window = SR // 5
    min_env = float(np.min(np.convolve(abs_w, np.ones(window) / window, mode="valid")))
    assert min_env > 0.05


def test_squeals_are_sparse_high_frequency_events():
    n = SR * 8
    sq = radio_fx._squeals(n, level=0.20, rng=np.random.default_rng(0))
    # Sparse: a large fraction of the timeline is near silence.
    silence_frac = float((np.abs(sq) < 0.005).sum()) / n
    assert silence_frac > 0.4
    # And the non-silent energy is concentrated above 1.4 kHz.
    f = np.fft.rfftfreq(n, 1 / SR)
    spec = np.abs(np.fft.rfft(sq))
    hi = float(spec[f > 1400].sum())
    lo = float(spec[f < 800].sum())
    assert hi > lo * 3


@needs_ffmpeg
def test_station_layer_never_goes_silent_when_asset_present():
    if radio_fx._interference_path() is None:
        pytest.skip("numbers-station asset not present")
    n = SR * 6
    s = radio_fx._station_layer(n, level=0.3, rng=np.random.default_rng(1))
    # Sliding-window envelope min should be above zero (continuous bleed).
    abs_s = np.abs(s)
    window = SR // 2
    min_env = float(np.min(np.convolve(abs_s, np.ones(window) / window, mode="valid")))
    assert min_env > 1e-4


def test_clicks_present_at_start_and_end():
    n = SR * 3
    c = radio_fx._clicks(n, level=0.5, rng=np.random.default_rng(0))
    head = float(np.sqrt(np.mean(c[: SR // 10] ** 2)))
    tail = float(np.sqrt(np.mean(c[-SR // 10:] ** 2)))
    mid = float(np.sqrt(np.mean(c[SR : 2 * SR] ** 2)))
    assert head > mid * 5
    assert tail > mid * 5


# ---------------------------------------------------------------- full pipeline
@needs_ffmpeg
def test_process_pcm_voice_forward_and_finite():
    rng = np.random.default_rng(123)
    voice = (0.4 * np.sin(2 * np.pi * 700 * np.arange(SR * 3) / SR)).astype(np.float32)
    out = radio_fx._process_pcm(voice, intensity=0.7, rng=rng)
    assert out.dtype == np.float32
    assert np.all(np.isfinite(out))
    assert float(np.max(np.abs(out))) <= 1.0 + 1e-3
    assert float(np.sqrt(np.mean(out * out))) > 0.05


@needs_ffmpeg
def test_process_pcm_intensity_changes_output():
    rng_a = np.random.default_rng(0)
    rng_b = np.random.default_rng(0)
    voice = (0.4 * np.sin(2 * np.pi * 700 * np.arange(SR * 2) / SR)).astype(np.float32)
    low = radio_fx._process_pcm(voice, intensity=0.1, rng=rng_a)
    high = radio_fx._process_pcm(voice, intensity=0.95, rng=rng_b)
    # Same seed but different intensity → different output.
    assert not np.array_equal(low, high)


def test_bundled_numbers_station_asset_present():
    path = radio_fx._interference_path()
    assert path is not None and path.is_file()
    assert path.stat().st_size > 50_000


# ---------------------------------------------------------------- entry guards
@pytest.mark.asyncio
async def test_apply_radio_effect_empty_input_returns_none():
    assert await radio_fx.apply_radio_effect(b"") is None


@pytest.mark.asyncio
async def test_apply_radio_effect_without_ffmpeg_returns_none(monkeypatch):
    monkeypatch.setattr(radio_fx, "ffmpeg_available", lambda: False)
    assert await radio_fx.apply_radio_effect(b"not-really-audio") is None


@pytest.mark.asyncio
async def test_apply_radio_effect_decode_failure_returns_none(monkeypatch):
    """If ffmpeg decode produces no PCM, the pipeline returns None
    instead of crashing or emitting an empty Opus file."""
    monkeypatch.setattr(radio_fx, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(radio_fx, "_decode_to_pcm",
                        lambda audio: np.zeros(0, dtype=np.float32))
    out = await radio_fx.apply_radio_effect(b"garbage", intensity=0.5)
    assert out is None
