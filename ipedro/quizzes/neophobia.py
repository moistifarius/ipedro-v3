"""Food Neophobia — the FNS.

A 10-item measure of the reluctance to eat unfamiliar foods; five items are
reverse-keyed (the neophilic ones).

    Pliner, P., & Hobden, K. (1992). Development of a scale to measure the trait
    of food neophobia in humans. Appetite, 19(2), 105-120.

Rated 1 (strongly disagree) to 7 (strongly agree); after reversing the neophilic
items, higher = pickier. The natural sibling to the food-disgust half of the
Disgust Test. For fun, not a diagnosis.
"""

from __future__ import annotations

from ipedro.quizzes.types import (
    Quiz, QuizItem, QuizResult, agreement_words, default_item_image_prompt,
    reverse_value,
)

_SCALE_MAX = 7

_ITEMS = (
    QuizItem("fns1", "🍢", "I am constantly sampling new and different foods.", reverse=True),
    QuizItem("fns2", "🤨", "I don't trust new foods.", reverse=False),
    QuizItem("fns3", "🕵️", "If I don't know what's in a food, I won't try it.", reverse=False),
    QuizItem("fns4", "🌏", "I like foods from different countries.", reverse=True),
    QuizItem("fns5", "😬", "Foreign food looks too weird to eat.", reverse=False),
    QuizItem("fns6", "🥂", "At dinner parties, I'll try a new food.", reverse=True),
    QuizItem("fns7", "😰", "I'm afraid to eat things I've never had before.", reverse=False),
    QuizItem("fns8", "📋", "I am very particular about the foods I'll eat.", reverse=False),
    QuizItem("fns9", "😋", "I will eat almost anything.", reverse=True),
    QuizItem("fns10", "🍜", "I like to try new restaurants.", reverse=True),
)


def _band(score: float) -> str:
    if score < 3:
        return "Adventurous Eater 🌏"
    if score < 4.5:
        return "Middle-of-the-menu 🍽"
    return "Picky Eater 🙅"


def _score(answers: list[int]) -> QuizResult:
    if len(answers) != len(_ITEMS) or any(a < 1 or a > _SCALE_MAX for a in answers):
        raise ValueError("bad answers")
    scored = [
        reverse_value(NEOPHOBIA, a) if item.reverse else a
        for item, a in zip(_ITEMS, answers)
    ]
    neo = round(sum(scored) / len(scored), 2)
    adventure = round(_SCALE_MAX + 1 - neo, 2)
    return QuizResult(
        headline=neo,
        headline_max=float(_SCALE_MAX),
        summary=_band(neo),
        meters=[
            ("🙅 Pickiness     ", neo / _SCALE_MAX, f"{neo}/7"),
            ("🌏 Adventurousness", adventure / _SCALE_MAX, f"{adventure}/7"),
        ],
        extras=[],
        image_subject=_band(neo),
        verdict_payload=(
            f"Food neophobia {neo}/7 ({_band(neo)}); adventurousness {adventure}/7. "
            "Higher neophobia = pickier / more suspicious of unfamiliar food."
        ),
        detail={"neophobia": neo, "adventurousness": adventure},
    )


def _result_image_prompt(result: QuizResult) -> str:
    return (
        "A cartoonish flat-vector food 'badge' illustration for an eater who is "
        f"'{result.summary}'. A plate with a mix of familiar and exotic foods, "
        "warm friendly palette, centered, no text, no words, wholesome sticker "
        "style."
    )


NEOPHOBIA = Quiz(
    id="neophobia",
    title="Food Neophobia",
    emoji="🍢",
    commands=("foodtest", "neophobia", "pickytest"),
    board_commands=("foodboard", "pickyboard"),
    blurb="picky ↔ adventurous eater",
    prompt_lead="How much do you agree?",
    scale_min=1,
    scale_max=_SCALE_MAX,
    scale_legend="1 = strongly disagree  ·  7 = strongly agree",
    scale_words=agreement_words(_SCALE_MAX),
    items=_ITEMS,
    citation="Food Neophobia Scale (Pliner & Hobden, 1992). For fun, not a diagnosis.",
    verdict_instruction=(
        "The user just took a food-neophobia test (picky vs adventurous eater). "
        "In character, react in at most two sentences to how adventurous or "
        "fussy an eater they are. Plain text, no markdown."
    ),
    score=_score,
    item_image_prompt=default_item_image_prompt,
    result_image_prompt=_result_image_prompt,
)
