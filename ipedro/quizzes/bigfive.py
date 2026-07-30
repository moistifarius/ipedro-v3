"""Big Five — the Ten-Item Personality Inventory (TIPI).

Ten items (two per trait, one reverse-keyed) covering the OCEAN model:
Openness, Conscientiousness, Extraversion, Agreeableness, and Emotional
Stability (the reverse of Neuroticism).

    Gosling, S. D., Rentfrow, P. J., & Swann, W. B. (2003). A very brief measure
    of the Big-Five personality domains. Journal of Research in Personality,
    37(6), 504-528.

Rated 1 (disagree strongly) to 7 (agree strongly). A profile, not a single
score — so there's no leaderboard. For fun, not a diagnosis.
"""

from __future__ import annotations

from ipedro.quizzes.types import (
    Quiz, QuizItem, QuizResult, agreement_words, default_item_image_prompt,
    reverse_value,
)

_SCALE_MAX = 7

# TIPI order. reverse flags per the scoring key (items 2,4,6,8,10 are reverse).
_ITEMS = (
    QuizItem("tipi1", "🎉", "Extraverted, enthusiastic.", trait="E"),
    QuizItem("tipi2", "😤", "Critical, quarrelsome.", trait="A", reverse=True),
    QuizItem("tipi3", "📋", "Dependable, self-disciplined.", trait="C"),
    QuizItem("tipi4", "😰", "Anxious, easily upset.", trait="S", reverse=True),
    QuizItem("tipi5", "🎨", "Open to new experiences, complex.", trait="O"),
    QuizItem("tipi6", "🤐", "Reserved, quiet.", trait="E", reverse=True),
    QuizItem("tipi7", "🫶", "Sympathetic, warm.", trait="A"),
    QuizItem("tipi8", "🌪️", "Disorganized, careless.", trait="C", reverse=True),
    QuizItem("tipi9", "🧘", "Calm, emotionally stable.", trait="S"),
    QuizItem("tipi10", "🧱", "Conventional, uncreative.", trait="O", reverse=True),
)

_LABEL = {
    "O": ("🎨 Openness      ", "Free Spirit 🎨"),
    "C": ("📋 Conscientious ", "Organizer 📋"),
    "E": ("🎉 Extraversion  ", "Extravert 🎉"),
    "A": ("🫶 Agreeableness ", "Sweetheart 🫶"),
    "S": ("🧘 Stability     ", "Unshakeable 🧘"),
}


def _score(answers: list[int]) -> QuizResult:
    if len(answers) != len(_ITEMS) or any(a < 1 or a > _SCALE_MAX for a in answers):
        raise ValueError("bad answers")

    def rv(a: int) -> int:
        return reverse_value(BIG_FIVE, a)

    traits = {
        "E": round((answers[0] + rv(answers[5])) / 2, 2),
        "A": round((rv(answers[1]) + answers[6]) / 2, 2),
        "C": round((answers[2] + rv(answers[7])) / 2, 2),
        "S": round((rv(answers[3]) + answers[8]) / 2, 2),
        "O": round((answers[4] + rv(answers[9])) / 2, 2),
    }
    dominant = max(traits.items(), key=lambda t: t[1])[0]
    return QuizResult(
        headline=traits["E"],   # stored, but this quiz is unranked
        headline_max=float(_SCALE_MAX),
        summary=_LABEL[dominant][1],
        meters=[
            (_LABEL[t][0], traits[t] / _SCALE_MAX, f"{traits[t]}/7")
            for t in ("O", "C", "E", "A", "S")
        ],
        extras=[f"Strongest trait: {_LABEL[dominant][1]}"],
        image_subject=_LABEL[dominant][1],
        verdict_payload=(
            "Big Five (1-7): Openness {O}, Conscientiousness {C}, Extraversion "
            "{E}, Agreeableness {A}, Emotional Stability {S}. Strongest: "
            f"{dominant}.".format(**traits)
        ),
        detail=traits,
    )


def _result_image_prompt(result: QuizResult) -> str:
    return (
        "A cartoonish flat-vector personality 'badge' illustration for someone "
        f"whose strongest trait makes them a '{result.summary}'. Friendly modern "
        "style, soft gradient palette, a single expressive character centered, "
        "no text, no words, wholesome sticker style."
    )


BIG_FIVE = Quiz(
    id="bigfive",
    title="Big Five",
    emoji="🌈",
    commands=("bigfive", "personality", "ocean"),
    board_commands=(),          # a 5-trait profile → no single-axis leaderboard
    blurb="your OCEAN personality profile",
    prompt_lead="I see myself as…",
    scale_min=1,
    scale_max=_SCALE_MAX,
    scale_legend="1 = disagree strongly  ·  7 = agree strongly",
    scale_words=agreement_words(_SCALE_MAX),
    items=_ITEMS,
    citation="Ten-Item Personality Inventory (Gosling et al., 2003). For fun, not a diagnosis.",
    verdict_instruction=(
        "The user just took the Big Five (TIPI) personality test. In character, "
        "give a short two-sentence read on their profile — lead with their "
        "strongest trait. Plain text, no markdown."
    ),
    score=_score,
    item_image_prompt=default_item_image_prompt,
    result_image_prompt=_result_image_prompt,
)
