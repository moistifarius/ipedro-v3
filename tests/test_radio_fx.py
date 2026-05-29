"""Tests for the radio-transmission audio effect (ffmpeg filtergraph).

We can't run ffmpeg in CI, so these cover the pure-python pieces: the
filtergraph/arg builders and the graceful-degradation guards.
"""

from __future__ import annotations

import pytest

from ipedro import radio_fx


def test_filtergraph_contains_core_filters():
    fg = radio_fx._build_filtergraph(0.5)
    # Band-limit, compress, normalize, presence, pitch drift, light crush,
    # soft-clip, smooth QSB fades, delay/reverb, mix.
    for needle in ("highpass", "lowpass", "acompressor", "dynaudnorm",
                   "equalizer", "acrusher", "asoftclip", "vibrato",
                   "tremolo", "aecho", "amix"):
        assert needle in fg, f"missing {needle!r}"
    # Two aecho stages: slapback + reverb tail.
    assert fg.count("aecho") == 2
    # Realism: the modulation is continuous — NO hard gt()/lt() gate logic
    # in the voice/whistle level paths (a single lt() click is allowed in
    # the static path, but no gt() mode-switch gates anywhere).
    assert "gt(" not in fg
    # Three-input mix (voice + static + heterodyne) into a single [out] pad.
    assert "[0:a]" in fg and "[1:a]" in fg and "[2:a]" in fg
    assert "[v]" in fg and "[n]" in fg and "[h]" in fg and "[out]" in fg
    assert "amix=inputs=3" in fg


def test_filtergraph_with_station_adds_fourth_layer():
    fg = radio_fx._build_filtergraph(0.5, with_station=True)
    assert "[3:a]" in fg and "[g]" in fg
    assert "amix=inputs=4" in fg


def test_filtergraph_intensity_changes_output():
    assert radio_fx._build_filtergraph(0.1) != radio_fx._build_filtergraph(0.9)


def test_filtergraph_intensity_is_clamped():
    # Out-of-range values must not raise and must match the nearest bound.
    assert radio_fx._build_filtergraph(-1.0) == radio_fx._build_filtergraph(0.0)
    assert radio_fx._build_filtergraph(5.0) == radio_fx._build_filtergraph(1.0)


def test_bundled_numbers_station_asset_present():
    # The interference bed is committed and non-trivial.
    path = radio_fx._interference_path()
    assert path is not None and path.is_file()
    assert path.stat().st_size > 50_000


def test_ffmpeg_args_request_ogg_opus_mono():
    args = radio_fx._ffmpeg_args(0.5)
    assert args[0] == "ffmpeg"
    assert "libopus" in args
    assert "pipe:0" in args and "pipe:1" in args
    # mono out
    assert args[args.index("-ac") + 1] == "1"
    # lavfi sources: white-noise static + FM-swept heterodyne whistle
    assert any("anoisesrc" in a for a in args)
    assert any(a.startswith("aevalsrc=") for a in args)
    # voice + 2 lavfi + (numbers station, since the asset is bundled) = 4 -i
    if radio_fx._interference_path() is not None:
        assert args.count("-i") == 4
        assert "-stream_loop" in args
    else:
        assert args.count("-i") == 3


def test_heterodyne_is_fm_swept_and_widens_with_intensity():
    lo = radio_fx._heterodyne_lavfi(0.0)
    hi = radio_fx._heterodyne_lavfi(1.0)
    # FM phase form: carrier + (dev/rate)*sin(...) — a true sweep, not a
    # static tone. Deviation grows with intensity.
    assert "aevalsrc=" in lo and "sin(" in lo
    assert lo != hi
    assert "400/0.15" in lo and "900/0.15" in hi


@pytest.mark.asyncio
async def test_apply_radio_effect_empty_input_returns_none():
    assert await radio_fx.apply_radio_effect(b"") is None


@pytest.mark.asyncio
async def test_apply_radio_effect_without_ffmpeg_returns_none(monkeypatch):
    monkeypatch.setattr(radio_fx, "ffmpeg_available", lambda: False)
    assert await radio_fx.apply_radio_effect(b"not-really-audio") is None
