"""Dark Triad — the "Dirty Dozen".

A 12-item measure of the three dark personality traits — Machiavellianism,
psychopathy, and narcissism — four items each.

    Jonason, P. K., & Webster, G. D. (2010). The dirty dozen: A concise measure
    of the dark triad. Psychological Assessment, 22(2), 420-432.

Rated 1 (strongly disagree) to 7 (strongly agree); higher = darker. For fun,
not a diagnosis.
"""

from __future__ import annotations

from ipedro.quizzes.types import Quiz, QuizItem, QuizResult, agreement_words

_SCALE_MAX = 7

# key, emoji, statement, section, trait, image search term
_ITEMS = (
    QuizItem("dd_mach1", "🎭", "I tend to manipulate others to get my way.", section="Machiavellianism", trait="mach", image_query="puppet strings"),
    QuizItem("dd_mach2", "🃏", "I have used deceit or lied to get my way.", section="Machiavellianism", trait="mach", image_query="joker card"),
    QuizItem("dd_mach3", "🍯", "I have used flattery to get my way.", section="Machiavellianism", trait="mach", image_query="honey jar"),
    QuizItem("dd_mach4", "♟️", "I tend to exploit others toward my own ends.", section="Machiavellianism", trait="mach", image_query="chess pieces"),
    QuizItem("dd_psy1", "🧊", "I tend to lack remorse.", section="Psychopathy", trait="psy", image_query="ice cube"),
    QuizItem("dd_psy2", "😶", "I tend to be unconcerned with the morality of my actions.", section="Psychopathy", trait="psy", image_query="blank mask"),
    QuizItem("dd_psy3", "🪨", "I tend to be callous or insensitive.", section="Psychopathy", trait="psy", image_query="boulder rock"),
    QuizItem("dd_psy4", "🙄", "I tend to be cynical.", section="Psychopathy", trait="psy", image_query="storm cloud"),
    QuizItem("dd_narc1", "✨", "I tend to want others to admire me.", section="Narcissism", trait="narc", image_query="applause crowd"),
    QuizItem("dd_narc2", "📣", "I tend to want others to pay attention to me.", section="Narcissism", trait="narc", image_query="megaphone"),
    QuizItem("dd_narc3", "👑", "I tend to seek prestige or status.", section="Narcissism", trait="narc", image_query="gold crown"),
    QuizItem("dd_narc4", "🎁", "I tend to expect special favors from others.", section="Narcissism", trait="narc", image_query="gift box"),
)

_TRAIT_LABEL = {"mach": "Schemer 🎭", "psy": "Cold Operator 🧊", "narc": "Spotlight Hog ✨"}
_TRAIT_QUERY = {"mach": "theatre mask", "psy": "iceberg", "narc": "gold crown"}


def _mean(xs: list[int]) -> float:
    return round(sum(xs) / len(xs), 2)


def _score(answers: list[int]) -> QuizResult:
    if len(answers) != len(_ITEMS) or any(a < 1 or a > _SCALE_MAX for a in answers):
        raise ValueError("bad answers")
    by = {"mach": [], "psy": [], "narc": []}
    for item, a in zip(_ITEMS, answers):
        by[item.trait].append(a)
    mach, psy, narc = _mean(by["mach"]), _mean(by["psy"]), _mean(by["narc"])
    overall = round(sum(answers) / len(answers), 2)

    dominant = max(("mach", mach), ("psy", psy), ("narc", narc), key=lambda t: t[1])[0]
    if overall < 3:
        level = "Mostly Harmless 😇"
    elif overall < 4.5:
        level = "A Bit Shady 😏"
    else:
        level = "Certified Menace 😈"

    return QuizResult(
        headline=overall,
        headline_max=float(_SCALE_MAX),
        summary=f"{level} · {_TRAIT_LABEL[dominant]}",
        meters=[
            ("🎭 Machiavellian", mach / _SCALE_MAX, f"{mach}/7"),
            ("🧊 Psychopathy  ", psy / _SCALE_MAX, f"{psy}/7"),
            ("✨ Narcissism   ", narc / _SCALE_MAX, f"{narc}/7"),
        ],
        extras=[f"Darkest streak: {_TRAIT_LABEL[dominant]}"],
        image_subject=_TRAIT_QUERY[dominant],
        verdict_payload=(
            f"Overall darkness {overall}/7 ({level}). Machiavellianism {mach}/7, "
            f"psychopathy {psy}/7, narcissism {narc}/7. Dominant: {dominant}."
        ),
        detail={"mach": mach, "psy": psy, "narc": narc, "overall": overall,
                "dominant": dominant},
    )


DARK_TRIAD = Quiz(
    id="darktriad",
    title="Dark Triad",
    emoji="😈",
    commands=("darktriad", "villain", "bastardtest"),
    board_commands=("darktriadboard", "villainboard"),
    blurb="how much of a bastard you are (Mach / psychopathy / narcissism)",
    prompt_lead="How much do you agree?",
    scale_min=1,
    scale_max=_SCALE_MAX,
    scale_legend="1 = strongly disagree  ·  7 = strongly agree",
    scale_words=agreement_words(_SCALE_MAX),
    items=_ITEMS,
    citation="Dark Triad 'Dirty Dozen' (Jonason & Webster, 2010). For fun, not a diagnosis.",
    verdict_instruction=(
        "The user just took a tongue-in-cheek Dark Triad ('how much of a "
        "bastard are you') test. In character, roast or congratulate them in at "
        "most two sentences based on how dark they scored and which trait "
        "dominates. Plain text, no markdown."
    ),
    score=_score,
)
