"""Tests for the ether (pager garbling) feature."""

from __future__ import annotations

import random

from ipedro.ether import _roll_intensity, _wrap, garble_pager


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
