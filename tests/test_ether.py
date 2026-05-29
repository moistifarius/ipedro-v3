"""Tests for the ether (pager garbling) feature."""

from __future__ import annotations

import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import ether
from ipedro.ether import (
    _pick_destination_any, _roll_intensity, _roll_radio_intensity, _wrap,
    garble_pager, manual_broadcast,
)


def test_garble_is_deterministic_with_seeded_rng() -> None:
    rng_a = random.Random(42)
    rng_b = random.Random(42)
    src = "hello there general kenobi, how are the troops today?"
    assert garble_pager(src, rng=rng_a) == garble_pager(src, rng=rng_b)


def test_garble_never_returns_empty() -> None:
    # Even a high-drop seed should leave at least the sentinel.
    out = garble_pager("", rng=random.Random(1))
    assert out == "***"
    # Whitespace-only input also resolves to sentinel.
    assert garble_pager("   ", rng=random.Random(1)) == "***"


def test_garble_truncates_oversize_input() -> None:
    src = "x" * 500
    # Output may be even shorter after random truncation, but never longer
    # than the 240-char pager cap plus a few chars for "…".
    out = garble_pager(src, rng=random.Random(0))
    assert len(out) <= 245


def test_garble_preserves_alphanumeric_density_roughly() -> None:
    src = "the quick brown fox jumps over the lazy dog " * 3
    out = garble_pager(src, rng=random.Random(7))
    # At least *some* of the original letters survive. Combined drop +
    # sub rate is around 18%, so the output should still be >60% the
    # length of the (pre-truncation) input on average.
    assert len(out) >= 40


def test_wrap_uses_one_of_known_templates() -> None:
    out = _wrap("BODY", rng=random.Random(3))
    assert "BODY" in out
    assert out.startswith("📟")


def test_higher_intensity_corrupts_more_than_lower_on_average() -> None:
    src = "the quick brown fox jumps over the lazy dog and then again"
    low_len_total = 0
    high_len_total = 0
    n = 60
    for seed in range(n):
        low_len_total += len(garble_pager(
            src, intensity=0.05, rng=random.Random(seed),
        ))
        high_len_total += len(garble_pager(
            src, intensity=0.95, rng=random.Random(seed),
        ))
    # On average, high intensity should produce strictly shorter output.
    assert high_len_total < low_len_total


def test_low_intensity_keeps_some_lowercase() -> None:
    # At intensity 0 the all-caps probability is 0.20; ~80% of words
    # should stay mixed-case across enough samples.
    src = "lorem ipsum dolor sit amet consectetur adipiscing elit"
    lowercase_word_seen = False
    for seed in range(30):
        out = garble_pager(src, intensity=0.0, rng=random.Random(seed))
        for w in out.split():
            if w and w.isalpha() and w != w.upper():
                lowercase_word_seen = True
                break
        if lowercase_word_seen:
            break
    assert lowercase_word_seen


def test_intensity_is_clamped_to_unit_interval() -> None:
    # Sentinel: out-of-range intensities don't crash and behave like the
    # nearest valid extreme.
    a = garble_pager("hello world example", intensity=-5.0, rng=random.Random(1))
    b = garble_pager("hello world example", intensity=0.0, rng=random.Random(1))
    assert a == b
    c = garble_pager("hello world example", intensity=10.0, rng=random.Random(2))
    d = garble_pager("hello world example", intensity=1.0, rng=random.Random(2))
    assert c == d


def test_roll_intensity_stays_within_advertised_range() -> None:
    rng = random.Random(123)
    for _ in range(200):
        v = _roll_intensity(rng=rng)
        assert 0.15 <= v <= 0.95


def test_roll_radio_intensity_is_high_biased() -> None:
    rng = random.Random(7)
    vals = [_roll_radio_intensity(rng=rng) for _ in range(400)]
    assert all(0.55 <= v <= 1.0 for v in vals)
    # Mode is 0.85, so the average should sit comfortably in the heavy band.
    assert sum(vals) / len(vals) > 0.7


def test_wrap_msg_code_template_has_zero_padded_code() -> None:
    # Drive the rng so we hit the MSG-{code} template.
    for seed in range(50):
        rng = random.Random(seed)
        out = _wrap("BODY", rng=rng)
        if "MSG-" in out:
            # Extract: "📟 MSG-0042\nBODY"
            code_part = out.split("MSG-", 1)[1].split("\n", 1)[0]
            assert len(code_part) == 4
            assert code_part.isdigit()
            return
    # If we never hit it in 50 seeds something's structurally wrong.
    raise AssertionError("MSG- template never selected in 50 seeds")


# --------------------------------------------------------------- manual /ether
def _fake_db(opted_in):
    """Minimal Database stub: fetch() returns the opted-in chat rows."""
    return SimpleNamespace(
        fetch=AsyncMock(return_value=[{"chat_id": c} for c in opted_in]),
        execute=AsyncMock(return_value="INSERT 0 1"),
    )


def _fake_bot():
    return SimpleNamespace(
        send_voice=AsyncMock(return_value=SimpleNamespace(message_id=111)),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=222)),
    )


def test_pick_destination_any_excludes_source():
    assert _pick_destination_any([5], exclude=5) is None
    assert _pick_destination_any([5, 9], exclude=5) == 9
    assert _pick_destination_any([], exclude=5) is None


@pytest.mark.asyncio
async def test_manual_broadcast_voice_path(monkeypatch):
    """A real voice note → radio FX → send_voice, cooldown stamped."""
    monkeypatch.setattr(ether, "apply_radio_effect",
                        AsyncMock(return_value=b"OGGTREATED"))
    db = _fake_db([100, 200])
    bot = _fake_bot()
    openai = SimpleNamespace(text_to_speech=AsyncMock())
    res = await manual_broadcast(
        bot, db, openai, source_chat_id=100, voice_bytes=b"rawvoice",
    )
    assert res.mode == "voice"
    assert res.dest_id == 200
    bot.send_voice.assert_awaited_once()
    openai.text_to_speech.assert_not_called()   # had real audio, no TTS
    db.execute.assert_awaited()                 # last_ether_at stamped


@pytest.mark.asyncio
async def test_manual_broadcast_text_uses_tts_then_fx(monkeypatch):
    monkeypatch.setattr(ether, "apply_radio_effect",
                        AsyncMock(return_value=b"OGGTREATED"))
    db = _fake_db([100, 200])
    bot = _fake_bot()
    openai = SimpleNamespace(text_to_speech=AsyncMock(return_value=b"mp3"))
    res = await manual_broadcast(
        bot, db, openai, source_chat_id=100, text="hello out there",
    )
    assert res.mode == "voice"
    openai.text_to_speech.assert_awaited_once()
    bot.send_voice.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_broadcast_falls_back_to_text(monkeypatch):
    """FX unavailable + we have text → garbled text broadcast."""
    monkeypatch.setattr(ether, "apply_radio_effect",
                        AsyncMock(return_value=None))
    db = _fake_db([100, 200])
    bot = _fake_bot()
    openai = SimpleNamespace(text_to_speech=AsyncMock(return_value=b"mp3"))
    res = await manual_broadcast(
        bot, db, openai, source_chat_id=100, text="hello out there",
    )
    assert res.mode == "text"
    bot.send_message.assert_awaited_once()
    bot.send_voice.assert_not_called()


@pytest.mark.asyncio
async def test_manual_broadcast_no_destination(monkeypatch):
    monkeypatch.setattr(ether, "apply_radio_effect", AsyncMock())
    db = _fake_db([100])   # only the source is opted in
    bot = _fake_bot()
    openai = SimpleNamespace(text_to_speech=AsyncMock())
    res = await manual_broadcast(
        bot, db, openai, source_chat_id=100, text="anybody?",
    )
    assert res.mode == "no_dest"
    bot.send_voice.assert_not_called()
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_manual_broadcast_voice_only_fx_fail_is_no_audio(monkeypatch):
    """Voice given, FX fails, no text to fall back on → no_audio."""
    monkeypatch.setattr(ether, "apply_radio_effect",
                        AsyncMock(return_value=None))
    db = _fake_db([100, 200])
    bot = _fake_bot()
    openai = SimpleNamespace(text_to_speech=AsyncMock())
    res = await manual_broadcast(
        bot, db, openai, source_chat_id=100, voice_bytes=b"rawvoice",
    )
    assert res.mode == "no_audio"
    bot.send_voice.assert_not_called()
    bot.send_message.assert_not_called()
