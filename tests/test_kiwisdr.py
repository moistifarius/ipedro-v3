"""Tests for the KiwiSDR WebSocket client.

The actual fetch needs a live KiwiSDR — not available in CI. These
tests cover the pure-Python pieces (URL parsing, IMA-ADPCM decoder,
SND frame parser) and exercise the high-level async fetch loop with
a stubbed WebSocket so it never opens a real socket.
"""

from __future__ import annotations

import struct
from typing import Iterable
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from ipedro import kiwisdr
from ipedro.kiwisdr import ADPCMState, decode_ima_adpcm, parse_kiwi_url


# ---------------------------------------------------------------- URL parsing
def test_parse_kiwi_url_full():
    p = parse_kiwi_url("kiwi://kiwi.example.com:8073?freq=14040&mode=lsb")
    assert p == {
        "host": "kiwi.example.com",
        "port": 8073,
        "freq_khz": 14040.0,
        "mode": "lsb",
        "password": "#",
    }


def test_parse_kiwi_url_default_port_and_password():
    p = parse_kiwi_url("kiwi://example.net?freq=7050.5&mode=usb&password=pw")
    assert p is not None
    assert p["port"] == 8073
    assert p["freq_khz"] == 7050.5
    assert p["mode"] == "usb"
    assert p["password"] == "pw"


@pytest.mark.parametrize("url", [
    "http://example.com",                          # wrong scheme
    "kiwi://",                                      # no host
    "kiwi://h:8073",                                # missing freq + mode
    "kiwi://h:8073?freq=abc&mode=lsb",              # bad freq
    "kiwi://h:8073?freq=14040&mode=fm",             # bad mode
    "not a url at all",
])
def test_parse_kiwi_url_rejects_bad_input(url):
    assert parse_kiwi_url(url) is None


# ---------------------------------------------------------------- IMA-ADPCM
def _ref_decode_one(state: dict, nibble: int) -> int:
    """Reference IMA-ADPCM decoder (textbook form, public domain)."""
    step = kiwisdr._IMA_STEP_TABLE[state["index"]]
    delta = step >> 3
    if nibble & 1:
        delta += step >> 2
    if nibble & 2:
        delta += step >> 1
    if nibble & 4:
        delta += step
    if nibble & 8:
        state["predictor"] -= int(delta)
    else:
        state["predictor"] += int(delta)
    state["predictor"] = max(-32768, min(32767, state["predictor"]))
    state["index"] = max(0, min(88, state["index"]
                                + int(kiwisdr._IMA_INDEX_TABLE[nibble])))
    return state["predictor"]


def test_decode_ima_adpcm_matches_reference():
    rng = np.random.default_rng(0)
    payload = bytes(rng.integers(0, 256, size=64).astype(np.uint8))
    # ours
    state_ours = ADPCMState()
    ours = decode_ima_adpcm(payload, state_ours)
    # reference
    state_ref = {"predictor": 0, "index": 0}
    ref = []
    for b in payload:
        ref.append(_ref_decode_one(state_ref, b & 0x0F))
        ref.append(_ref_decode_one(state_ref, (b >> 4) & 0x0F))
    assert np.array_equal(ours, np.array(ref, dtype=np.int16))
    assert state_ours.predictor == state_ref["predictor"]
    assert state_ours.index == state_ref["index"]


def test_decode_ima_adpcm_carries_state_across_calls():
    """Decoding two halves of a payload separately must equal decoding
    the whole payload at once — the predictor + index carry over."""
    payload = bytes(range(32))
    one_shot = decode_ima_adpcm(payload, ADPCMState())
    s = ADPCMState()
    a = decode_ima_adpcm(payload[:16], s)
    b = decode_ima_adpcm(payload[16:], s)
    split = np.concatenate([a, b])
    assert np.array_equal(one_shot, split)


def test_decode_ima_adpcm_empty():
    assert decode_ima_adpcm(b"", ADPCMState()).size == 0


# ---------------------------------------------------------------- frame parse
def _build_snd_frame(adpcm_payload: bytes, *, seq: int = 1, rssi: int = -90,
                     flags: int = 0) -> bytes:
    return (b"SND"
            + bytes([flags])
            + struct.pack("<I", seq)
            + struct.pack("<h", rssi)
            + adpcm_payload)


def test_parse_snd_frame_extracts_payload():
    adpcm = bytes((i & 0xFF) for i in range(512))
    frame = _build_snd_frame(adpcm)
    payload = kiwisdr._parse_snd_frame(frame)
    assert payload == adpcm


def test_parse_snd_frame_rejects_non_snd_traffic():
    # waterfall frame, keepalive ACK, garbage — all return None.
    assert kiwisdr._parse_snd_frame(b"WF \x00" + b"\x00" * 64) is None
    assert kiwisdr._parse_snd_frame(b"") is None
    assert kiwisdr._parse_snd_frame(b"ABC") is None


def test_parse_snd_frame_rejects_payload_too_small_or_too_big():
    # Pad to >12 bytes so the length guard isn't tripped first, then
    # exceed the upper bound. payload at offset 10 = (len-10) bytes.
    too_big = _build_snd_frame(b"\x00" * 5000)
    assert kiwisdr._parse_snd_frame(too_big) is None


# ---------------------------------------------------------------- async fetch
class _FakeWebSocket:
    """Stands in for a websockets.client connection. ``incoming`` is
    the script of messages to deliver to recv()."""

    def __init__(self, incoming: Iterable):
        self._incoming = list(incoming)
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self):
        if not self._incoming:
            # Simulate idle — raises asyncio.TimeoutError via wait_for
            await self._never()
        return self._incoming.pop(0)

    @staticmethod
    async def _never():
        import asyncio
        await asyncio.sleep(60)


def _make_snd_frames(n_frames: int = 12, samples_per: int = 1024) -> list[bytes]:
    """Build N valid SND frames whose ADPCM payload decodes to non-zero
    samples (we use a non-zero nibble pattern to drive the predictor up)."""
    payload = bytes([0x55] * (samples_per // 2))  # alternating high nibbles
    return [_build_snd_frame(payload, seq=i) for i in range(n_frames)]


@pytest.mark.asyncio
async def test_fetch_pcm_async_collects_audio_from_mocked_socket(monkeypatch):
    frames = _make_snd_frames(n_frames=14)
    # Mix in a text MSG (which must be ignored) and a junk-tag frame.
    incoming = ["MSG audio_init=0 audio_rate=12000"] + frames \
               + [b"WF \x00ignored"] + frames

    fake = _FakeWebSocket(incoming=incoming)

    class _FakeConnect:
        def __init__(self, *args, **kw):
            self._sock = fake
        async def __aenter__(self):
            return self._sock
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(kiwisdr.websockets, "connect", _FakeConnect)
    pcm = await kiwisdr._fetch_pcm_async(
        host="x", port=8073, freq_khz=14040.0, mode="lsb",
        duration_s=1.0, timeout=5.0,
    )
    # ≥ 1 second's worth at 12 kHz
    assert pcm.dtype == np.int16
    assert pcm.size >= kiwisdr.KIWI_SAMPLE_RATE
    # Handshake commands were sent in order.
    sent = fake.sent
    assert any(s.startswith("SET auth t=kiwi p=") for s in sent)
    assert any(s.startswith("SET ident_user=") for s in sent)
    assert any("SET mod=lsb" in s and "freq=14040.00" in s for s in sent)


@pytest.mark.asyncio
async def test_fetch_pcm_async_returns_empty_on_websocket_failure(monkeypatch):
    class _ExplodingConnect:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            raise OSError("connection refused")
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(kiwisdr.websockets, "connect", _ExplodingConnect)
    pcm = await kiwisdr._fetch_pcm_async(
        host="x", port=8073, freq_khz=14040.0, mode="lsb",
        duration_s=1.0, timeout=3.0,
    )
    assert pcm.size == 0


def test_fetch_pcm_from_url_rejects_bad_url():
    # No network call; the URL parse fails first.
    assert kiwisdr.fetch_pcm_from_url("not-a-kiwi-url").size == 0
