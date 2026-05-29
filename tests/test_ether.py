"""Tests for the ether (pager garbling) feature."""

from __future__ import annotations

import random

from ipedro.ether import _wrap, garble_pager


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
