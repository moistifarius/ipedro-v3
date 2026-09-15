"""Scoring/bands/rendering for the disgust personality test.

Pins the deterministic half of the feature: item bank shape, subscale maths,
biggest-ick/iron-stomach selection, qualitative bands, and the caption cap.
"""

from __future__ import annotations

import pytest

from ipedro import disgust_test as dt


def test_item_bank_shape():
    assert dt.N_ITEMS == 16
    assert len(dt.FOOD_ITEMS) == 8 and len(dt.GENERAL_ITEMS) == 8
    # Food section = 8 distinct FDS subscales.
    assert len({it.subscale for it in dt.FOOD_ITEMS}) == 8
    # General section = the three DS-R domains, 3/3/2.
    domains = [it.subscale for it in dt.GENERAL_ITEMS]
    assert domains.count("core") == 3
    assert domains.count("animal_reminder") == 3
    assert domains.count("contamination") == 2
    # Every subscale has a pretty label and every item an emoji.
    for it in dt.ALL_ITEMS:
        assert it.subscale in dt.SUBSCALE_LABELS
        assert it.emoji


def test_section_and_domain_means():
    answers = [2] * 8 + [4] * 8            # food all 2, general all 4
    r = dt.score(answers)
    assert r.food_score == 2.0
    assert r.general_score == 4.0
    assert r.core_score == 4.0
    assert r.animal_score == 4.0
    assert r.contam_score == 4.0
    assert r.overall_score == 3.0          # mean of all 16


def test_biggest_ick_and_iron_stomach_pick_food_extremes():
    # Food: strongest reaction at index 3 (mould), weakest at index 6 (fish).
    food = [3, 3, 3, 6, 3, 3, 1, 3]
    r = dt.score(food + [3] * 8)
    assert r.biggest_ick_label == "mould"
    assert r.biggest_ick_emoji == "🧀"
    assert r.iron_stomach_label == "fish"
    assert r.iron_stomach_emoji == "🐟"


@pytest.mark.parametrize("val,expected", [
    (1.0, "iron-stomached"),
    (1.99, "iron-stomached"),
    (2.0, "pretty unbothered"),
    (2.99, "pretty unbothered"),
    (3.0, "middle of the road"),
    (3.99, "middle of the road"),
    (4.0, "squeamish"),
    (4.99, "squeamish"),
    (5.0, "can barely cope"),
    (6.0, "can barely cope"),
])
def test_band_boundaries(val, expected):
    assert dt.band(val) == expected


def test_all_min_and_all_max():
    lo = dt.score([1] * 16)
    hi = dt.score([6] * 16)
    assert lo.overall_score == 1.0 and lo.overall_band == "iron-stomached"
    assert hi.overall_score == 6.0 and hi.overall_band == "can barely cope"


@pytest.mark.parametrize("bad", [
    [1] * 15,             # too few
    [1] * 17,             # too many
    [0] + [1] * 15,       # below range
    [7] + [1] * 15,       # above range
])
def test_score_rejects_malformed(bad):
    with pytest.raises(ValueError):
        dt.score(bad)

