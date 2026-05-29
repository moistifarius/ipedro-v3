"""Tests for the radio-transmission audio effect (ffmpeg filtergraph).

We can't run ffmpeg in CI, so these cover the pure-python pieces: the
filtergraph/arg builders and the graceful-degradation guards.
"""

from __future__ import annotations

import pytest

from ipedro import radio_fx


def test_filtergraph_contains_core_filters():
    fg = radio_fx._build_filtergraph(0.5)
    # Band-limiting, crush/clip, pitch drift, fading, AGC, mix.
    for needle in ("highpass", "lowpass", "acrusher", "asoftclip",
                   "vibrato", "tremolo", "compand", "amix"):
        assert needle in fg, f"missing {needle!r}"
    # Three-input mix (voice + static + heterodyne) into a single [out] pad.
    assert "[0:a]" in fg and "[1:a]" in fg and "[2:a]" in fg
    assert "[v]" in fg and "[n]" in fg and "[h]" in fg and "[out]" in fg


def test_filtergraph_intensity_changes_output():
    assert radio_fx._build_filtergraph(0.1) != radio_fx._build_filtergraph(0.9)


def test_filtergraph_intensity_is_clamped():
    # Out-of-range values must not raise and must match the nearest bound.
    assert radio_fx._build_filtergraph(-1.0) == radio_fx._build_filtergraph(0.0)
    assert radio_fx._build_filtergraph(5.0) == radio_fx._build_filtergraph(1.0)


def test_ffmpeg_args_request_ogg_opus_mono():
    args = radio_fx._ffmpeg_args(0.5)
    assert args[0] == "ffmpeg"
    assert "libopus" in args
    assert "pipe:0" in args and "pipe:1" in args
    # mono out
    assert args[args.index("-ac") + 1] == "1"
    # two lavfi sources: white-noise static + heterodyne sine carrier
    assert any("anoisesrc" in a for a in args)
    assert any(a.startswith("sine=") for a in args)
    # the voice + 2 lavfi inputs = three '-i' flags
    assert args.count("-i") == 3


@pytest.mark.asyncio
async def test_apply_radio_effect_empty_input_returns_none():
    assert await radio_fx.apply_radio_effect(b"") is None


@pytest.mark.asyncio
async def test_apply_radio_effect_without_ffmpeg_returns_none(monkeypatch):
    monkeypatch.setattr(radio_fx, "ffmpeg_available", lambda: False)
    assert await radio_fx.apply_radio_effect(b"not-really-audio") is None
