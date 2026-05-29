"""Tests for the radio-transmission audio effect (ffmpeg filtergraph).

We can't run ffmpeg in CI, so these cover the pure-python pieces: the
filtergraph/arg builders and the graceful-degradation guards.
"""

from __future__ import annotations

import pytest

from ipedro import radio_fx


def test_filtergraph_contains_core_filters():
    fg = radio_fx._build_filtergraph(0.5)
    for needle in ("highpass", "lowpass", "acrusher", "tremolo",
                   "amix", "volume"):
        assert needle in fg
    # Two-input mix (voice + noise) into a single [out] pad.
    assert "[0:a]" in fg and "[1:a]" in fg and "[out]" in fg


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
    # the pink-noise lavfi source is the second input
    assert any("anoisesrc" in a for a in args)


@pytest.mark.asyncio
async def test_apply_radio_effect_empty_input_returns_none():
    assert await radio_fx.apply_radio_effect(b"") is None


@pytest.mark.asyncio
async def test_apply_radio_effect_without_ffmpeg_returns_none(monkeypatch):
    monkeypatch.setattr(radio_fx, "ffmpeg_available", lambda: False)
    assert await radio_fx.apply_radio_effect(b"not-really-audio") is None
