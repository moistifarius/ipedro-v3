"""Pure-function tests for duckhunt scoring."""

from __future__ import annotations

import random

from ipedro.duckhunt.scoring import (
    bang_outcome, bef_dice_passes, bef_refusal_outcome, bef_success_outcome,
    ignore_outcome, roll_rarity, RARITY_TIERS,
)


def test_roll_rarity_is_neutralized_to_common():
    # Rarity is currently disabled in gameplay: every spawn is tagged
    # 'common' regardless of seed or holiday flag.
    for seed in range(20):
        assert roll_rarity(random.Random(seed)) == "common"
    assert roll_rarity(random.Random(0), on_holiday=True) == "common"


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


def test_hit_awards_flat_points_regardless_of_rarity():
    # Rarity neutralized: bang_outcome's points_delta is the same flat
    # value no matter what rarity is passed in.
    class FixedLow:
        def random(self_inner):
            return 0.0  # always hits

    common = bang_outcome("common", 0, FixedLow())
    legendary = bang_outcome("legendary", 0, FixedLow())
    assert legendary.points_delta == common.points_delta == 1
    # Message no longer leaks rarity.
    assert "common" not in common.message
    assert "legendary" not in legendary.message


# ----------------------------------------------------------------- bef
def test_bef_dice_always_passes_while_rarity_neutralized():
    # The pre-AI dice gate used to be rarity-biased. It now always passes,
    # leaving the outcome to the AI verdict alone.
    class HighRoll:
        def random(self_inner):
            return 0.999

    assert bef_dice_passes("common", HighRoll()) is True
    assert bef_dice_passes("legendary", HighRoll()) is True


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
    # Message no longer mentions rarity tier (neutralized).
    assert "rare" not in out.message.lower()
    assert "befriended" in out.message.lower()
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


def test_ignore_notice_chance_is_flat_across_rarities():
    # Used to scale (common 0.15 → legendary 0.75). Now flat 0.20.
    # An RNG returning 0.5 wanders off for every rarity.
    class HighRoll:
        def random(self_inner):
            return 0.50  # > 0.20 threshold for all rarities
        def choice(self_inner, seq):
            return seq[0]

    for r in ("common", "uncommon", "rare", "epic", "legendary"):
        assert ignore_outcome(r, HighRoll()).resolves_duck is True


# ----------------------------------------------------------------- berate is gone
def test_berate_helpers_no_longer_exported():
    import ipedro.duckhunt.scoring as s

    assert not hasattr(s, "berate_outcome_from_judge")
