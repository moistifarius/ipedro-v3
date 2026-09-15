"""Pure scoring for the Dark Triad, Food Neophobia, and Big Five quizzes.

Pins the maths that varies per quiz: reverse-keying, trait means, dominant-trait
labels, and input validation.
"""

from __future__ import annotations

import pytest

from ipedro.quizzes.bigfive import BIG_FIVE
from ipedro.quizzes.darktriad import DARK_TRIAD
from ipedro.quizzes.neophobia import NEOPHOBIA
from ipedro.quizzes.types import reverse_value


def test_reverse_value_flips_on_scale():
    assert reverse_value(DARK_TRIAD, 1) == 7
    assert reverse_value(DARK_TRIAD, 7) == 1
    assert reverse_value(DARK_TRIAD, 4) == 4


# --------------------------------------------------------------- Dark Triad
def test_dark_triad_levels_and_dominant():
    harmless = DARK_TRIAD.score([1] * 12)
    assert harmless.headline == 1.0
    assert "Mostly Harmless" in harmless.summary

    menace = DARK_TRIAD.score([7] * 12)
    assert menace.headline == 7.0
    assert "Certified Menace" in menace.summary

    # Machiavellianism items are the first four; spike them.
    mach = DARK_TRIAD.score([7, 7, 7, 7] + [1] * 8)
    assert "Schemer" in mach.summary and "Shady" in mach.summary
    # Narcissism items are the last four.
    narc = DARK_TRIAD.score([1] * 8 + [7, 7, 7, 7])
    assert "Spotlight Hog" in narc.summary


@pytest.mark.parametrize("bad", [[1] * 11, [1] * 13, [8] + [1] * 11, [0] + [1] * 11])
def test_dark_triad_rejects_malformed(bad):
    with pytest.raises(ValueError):
        DARK_TRIAD.score(bad)


# --------------------------------------------------------------- Neophobia
def test_neophobia_reverse_scoring():
    # Reverse-keyed (neophilic) items are indices 0,3,5,8,9.
    rev = {0, 3, 5, 8, 9}
    # A true adventurer AGREES with neophilic items and DISAGREES with the rest.
    adventurous = [7 if i in rev else 1 for i in range(10)]
    r = NEOPHOBIA.score(adventurous)
    assert r.headline == 1.0
    assert "Adventurous" in r.summary

    # A picky eater is the mirror image.
    picky = [1 if i in rev else 7 for i in range(10)]
    r2 = NEOPHOBIA.score(picky)
    assert r2.headline == 7.0
    assert "Picky" in r2.summary

    mid = NEOPHOBIA.score([4] * 10)
    assert mid.headline == 4.0
    assert "Middle" in mid.summary


@pytest.mark.parametrize("bad", [[1] * 9, [1] * 11, [8] + [1] * 9])
def test_neophobia_rejects_malformed(bad):
    with pytest.raises(ValueError):
        NEOPHOBIA.score(bad)


# --------------------------------------------------------------- Big Five
def test_big_five_reverse_pairs_and_dominant():
    neutral = BIG_FIVE.score([4] * 10)
    assert all(abs(v - 4.0) < 1e-9 for v in neutral.detail.values())

    # Extraversion: item0 (E+) high, item5 (E- reverse) low → E maxes at 7.
    extravert = [7, 4, 4, 4, 4, 1, 4, 4, 4, 4]
    r = BIG_FIVE.score(extravert)
    assert r.detail["E"] == 7.0
    assert "Extravert" in r.summary

    # Openness: item4 (O+) high, item9 (O- reverse) low.
    opener = [4, 4, 4, 4, 7, 4, 4, 4, 4, 1]
    r2 = BIG_FIVE.score(opener)
    assert r2.detail["O"] == 7.0
    assert "Free Spirit" in r2.summary


@pytest.mark.parametrize("bad", [[4] * 9, [4] * 11, [0] + [4] * 9])
def test_big_five_rejects_malformed(bad):
    with pytest.raises(ValueError):
        BIG_FIVE.score(bad)


def test_all_quizzes_meters_have_valid_fractions():
    for quiz in (DARK_TRIAD, NEOPHOBIA, BIG_FIVE):
        mid = (quiz.scale_min + quiz.scale_max) // 2
        result = quiz.score([mid] * quiz.n_items)
        for _label, frac, _text in result.meters:
            assert 0.0 <= frac <= 1.0
