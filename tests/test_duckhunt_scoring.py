"""Pure-function tests for duckhunt scoring."""

from __future__ import annotations

import random

from ipedro.duckhunt.scoring import (
    bang_outcome, bef_dice_passes, bef_refusal_outcome, bef_success_outcome,
    ignore_outcome, roll_rarity, RARITY_TIERS,
)


def test_roll_rarity_returns_known_tier():
    for seed in range(20):
        name = roll_rarity(random.Random(seed))
        assert name in {t[0] for t in RARITY_TIERS}


def test_bang_hit_streak_bonus_makes_hits_more_likely():
    # Constant-RNG: roll = 0.85 misses on a 0.80 base for "common" but
    # passes with a 5-streak bonus (0.80 + 5*0.02 = 0.90).
    class FixedRng:
        def random(self_inner):
            return 0.85

    streak_zero = bang_outcome("common", 0, FixedRng())
    streak_five = bang_outcome("common", 5, FixedRng())
    assert streak_zero.success is False
    assert streak_five.success is True


def test_bang_miss_resets_streak_via_negative_delta():
    class AlwaysMiss:
        def random(self_inner):
            return 0.99

    out = bang_outcome("common", 7, AlwaysMiss())
    assert out.success is False
    assert out.streak_delta == -7
    assert out.resolves_duck is False


def test_legendary_hit_awards_more_points_than_common():
    class FixedLow:
        def random(self_inner):
            return 0.0  # always hits

    common = bang_outcome("common", 0, FixedLow())
    legendary = bang_outcome("legendary", 0, FixedLow())
    assert legendary.points_delta > common.points_delta


# ----------------------------------------------------------------- bef
def test_bef_dice_legendary_harder_than_common():
    class HighRoll:
        def random(self_inner):
            return 0.8  # passes common (0.85), fails legendary (0.15)

    assert bef_dice_passes("common", HighRoll()) is True
    assert bef_dice_passes("legendary", HighRoll()) is False


def test_bef_success_uses_ai_line_when_present():
    out = bef_success_outcome("common", "The duck nods solemnly. Friendship engaged.")
    assert out.success is True
    assert out.resolves_duck is True
    assert "Friendship engaged" in out.message
    assert out.streak_delta == 1
    assert out.points_delta >= 1


def test_bef_success_falls_back_to_default_message():
    out = bef_success_outcome("rare", None)
    assert out.success is True
    assert "rare" in out.message
    assert out.resolves_duck is True


def test_bef_refusal_does_not_resolve_duck():
    out = bef_refusal_outcome("Nope, the duck has standards.")
    assert out.success is False
    assert out.resolves_duck is False  # critical: duck STAYS on refusal
    assert out.points_delta == 0
    assert out.streak_delta == 0
    assert "standards" in out.message


def test_bef_refusal_default_message_used_when_ai_silent():
    out = bef_refusal_outcome(None)
    assert "doesn't want to be friends" in out.message.lower()


# ----------------------------------------------------------------- ignore
def test_ignore_sometimes_noticed_and_keeps_duck():
    class LowRoll:
        def random(self_inner):
            return 0.0  # always under noticed_chance
        def choice(self_inner, seq):
            return seq[0]

    out = ignore_outcome("common", LowRoll())
    assert out.success is True
    assert out.resolves_duck is False  # duck noticed and stayed
    assert out.points_delta == 0


def test_ignore_usually_wanders_off_for_common_duck():
    class HighRoll:
        def random(self_inner):
            return 0.99  # over noticed_chance for all rarities except legendary
        def choice(self_inner, seq):
            return seq[0]

    out = ignore_outcome("common", HighRoll())
    assert out.resolves_duck is True


def test_ignore_legendary_more_likely_to_notice():
    # Pick an RNG value that's between legendary's 0.75 and rare's 0.40.
    class FixedRng:
        def random(self_inner):
            return 0.60
        def choice(self_inner, seq):
            return seq[0]

    legendary = ignore_outcome("legendary", FixedRng())  # 0.6 <= 0.75 -> noticed (stays)
    rare = ignore_outcome("rare", FixedRng())            # 0.6 >  0.40 -> wanders (resolves)
    assert legendary.resolves_duck is False
    assert rare.resolves_duck is True


# ----------------------------------------------------------------- berate is gone
def test_berate_helpers_no_longer_exported():
    import ipedro.duckhunt.scoring as s

    assert not hasattr(s, "berate_outcome_from_judge")
