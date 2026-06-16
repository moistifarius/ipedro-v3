"""Tests for the challenge time-limit clock.

The trivia/captcha/recipe challenges are now timed so a player can't open
a tab and Google the answer. Enforcement is pure logic in
ipedro.duckhunt.scoring (issuer announces the clock, judge fails late
answers), so it's trivially unit-testable here.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from ipedro.duckhunt.scoring import (
    CHALLENGE_TIME_LIMITS,
    challenge_is_over_time,
    challenge_seconds_elapsed,
    challenge_time_limit_seconds,
    over_time_line,
)


def test_time_limit_per_kind_and_default():
    # Trivia is the tightest — that's the whole point (no Googling).
    assert challenge_time_limit_seconds("trivia") == CHALLENGE_TIME_LIMITS["trivia"]
    assert challenge_time_limit_seconds("captcha") == CHALLENGE_TIME_LIMITS["captcha"]
    assert challenge_time_limit_seconds("recipe") == CHALLENGE_TIME_LIMITS["recipe"]
    # Trivia strictly tighter than recipe so the clock actually bites.
    assert (
        challenge_time_limit_seconds("trivia")
        < challenge_time_limit_seconds("recipe")
    )
    # Unknown kind falls back to a sane default, not a crash.
    assert challenge_time_limit_seconds("nonsense") == 45


def test_seconds_elapsed_handles_naive_and_missing():
    # Missing created_at → 0.0 so a clockless challenge never times out.
    assert challenge_seconds_elapsed(None) == 0.0
    # Naive datetime is treated as UTC (no tz math blow-up).
    now = datetime(2026, 1, 1, 12, 0, 30, tzinfo=timezone.utc)
    naive = datetime(2026, 1, 1, 12, 0, 0)  # 30s earlier, naive
    assert challenge_seconds_elapsed(naive, now) == 30.0


def test_over_time_true_past_limit_false_within():
    now = datetime(2026, 1, 1, 12, 1, 0, tzinfo=timezone.utc)
    limit = challenge_time_limit_seconds("trivia")
    # Issued well within the limit → not over time.
    fresh = now - timedelta(seconds=limit - 5)
    assert challenge_is_over_time("trivia", fresh, now) is False
    # Issued past the limit → over time.
    stale = now - timedelta(seconds=limit + 5)
    assert challenge_is_over_time("trivia", stale, now) is True


def test_over_time_uses_the_kind_specific_limit():
    now = datetime(2026, 1, 1, 12, 5, 0, tzinfo=timezone.utc)
    # An age that's over the trivia limit but under the recipe limit.
    age = (
        challenge_time_limit_seconds("trivia")
        + challenge_time_limit_seconds("recipe")
    ) // 2
    issued = now - timedelta(seconds=age)
    assert challenge_is_over_time("trivia", issued, now) is True
    assert challenge_is_over_time("recipe", issued, now) is False


def test_missing_created_at_never_times_out():
    assert challenge_is_over_time("trivia", None) is False


def test_over_time_line_comes_from_the_pool():
    from ipedro.duckhunt.scoring import _OVER_TIME_FLAVOR

    rng = random.Random(0)
    for _ in range(50):
        assert over_time_line(rng) in _OVER_TIME_FLAVOR
