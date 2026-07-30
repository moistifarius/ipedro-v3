"""The disgust test, expressed as a Quiz on the engine.

The scoring, item bank, citations and image prompts still live in
`ipedro.disgust_test` (and keep their own unit tests); this module just adapts
them to the generic engine.
"""

from __future__ import annotations

from ipedro import disgust_test as dt
from ipedro.quizzes.types import Quiz, QuizItem, QuizResult

_VERDICT_INSTRUCTION = (
    "The user just finished a tongue-in-cheek disgust-sensitivity personality "
    "test. In character, give them a short, punchy verdict — at most two "
    "sentences. React to their result (how squeamish they are, their biggest "
    "'ick') instead of reading numbers back like a robot. Plain text, no "
    "markdown, no emoji spam."
)

_ITEMS = tuple(
    QuizItem(
        key=it.key, emoji=it.emoji, text=it.text,
        section="Food disgust" if it.section == "food" else "General disgust",
        trait=it.subscale,
    )
    for it in dt.ALL_ITEMS
)


def _score(answers: list[int]) -> QuizResult:
    r = dt.score(answers)   # raises ValueError on malformed input
    band_emoji = dt.BAND_EMOJI.get(r.overall_band, "")
    return QuizResult(
        headline=r.overall_score,
        headline_max=6.0,
        summary=f"{r.overall_band} {band_emoji}".strip(),
        meters=[
            ("🍽 Food   ", r.food_score / 6, f"{r.food_score}/6 · {r.food_band}"),
            ("🧠 General", r.general_score / 6, f"{r.general_score}/6 · {r.general_band}"),
            ("   core   ", r.core_score / 6, f"{r.core_score}/6"),
            ("   animal ", r.animal_score / 6, f"{r.animal_score}/6"),
            ("   contam ", r.contam_score / 6, f"{r.contam_score}/6"),
        ],
        extras=[
            f"Biggest ick: {r.biggest_ick_label} {r.biggest_ick_emoji}   "
            f"Iron stomach: {r.iron_stomach_label} {r.iron_stomach_emoji}",
        ],
        image_subject=f"{r.biggest_ick_emoji} {r.biggest_ick_label}",
        verdict_payload=(
            f"Overall: {r.overall_band} ({r.overall_score}/6). Food disgust "
            f"{r.food_score}/6 ({r.food_band}). General {r.general_score}/6 "
            f"({r.general_band}). Biggest ick: {r.biggest_ick_label}. "
            f"Iron stomach: {r.iron_stomach_label}."
        ),
        detail={
            "food": r.food_score, "general": r.general_score, "core": r.core_score,
            "animal": r.animal_score, "contam": r.contam_score,
            "overall": r.overall_score, "biggest_ick": r.biggest_ick_label,
        },
    )


def _item_image_prompt(quiz: Quiz, item: QuizItem) -> str:
    src = next((x for x in dt.ALL_ITEMS if x.key == item.key), None)
    return dt.item_image_prompt(src) if src else item.text


def _result_image_prompt(result: QuizResult) -> str:
    return (
        "A clean cartoonish flat-vector 'lab report' badge illustration for "
        f"someone who is '{result.summary}' about disgusting things, with a "
        f"playful motif of {result.image_subject}. Muted mint-and-cream lab "
        "palette, a small specimen jar and clipboard, friendly and clinical, "
        "centered, no text, no words, no gore, sticker style."
    )


DISGUST = Quiz(
    id="disgust",
    title="Disgust Test",
    emoji="🧫",
    commands=("disgusttest", "icktest", "disgust"),
    board_commands=("disgustboard", "ickboard"),
    blurb="how easily grossed out you are (food + the macabre)",
    prompt_lead="How grossed out would you be by…",
    scale_min=dt.SCALE_MIN,
    scale_max=dt.SCALE_MAX,
    scale_legend=dt.SCALE_LEGEND,
    scale_words=dt.SCALE_WORDS,
    items=_ITEMS,
    citation=dt.CITATION_SHORT,
    verdict_instruction=_VERDICT_INSTRUCTION,
    score=_score,
    item_image_prompt=_item_image_prompt,
    result_image_prompt=_result_image_prompt,
)
