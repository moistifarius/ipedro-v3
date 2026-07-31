"""The disgust test, expressed as a Quiz on the engine.

Scoring, item bank and citations live in `ipedro.disgust_test` (with their own
unit tests); this module adapts them to the generic engine and attaches a
web-image search term to each item.
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

# Concrete web-image search term per item (falls back to the scenario text).
_QUERIES = {
    "cartilage": "chicken cartilage", "silverware": "dirty cutlery",
    "shared_spoon": "soup spoon", "mould_cheese": "mouldy cheese",
    "brown_apple": "sliced apple", "brown_avocado": "brown avocado",
    "fish_texture": "raw fish fillet", "salad_snail": "snail leaf",
    "maggots": "maggots", "bin_stench": "garbage bin",
    "vomit": "sick bucket", "hand_jar": "specimen jar",
    "dead_body": "morgue", "glass_eye": "glass eye",
    "shared_glass": "drinking glass", "worn_shirt": "old shirt",
}

# Search term for the result-card image, by which food subscale iced them most.
_SUBSCALE_QUERY = {
    "animal_flesh": "raw meat", "poor_hygiene": "dirty dishes",
    "human_contamination": "shared spoon", "mould": "mouldy cheese",
    "decaying_fruit": "rotten fruit", "decaying_vegetables": "brown avocado",
    "fish": "raw fish", "living_contaminants": "snail",
}

_ITEMS = tuple(
    QuizItem(
        key=it.key, emoji=it.emoji, text=it.text,
        section="Food disgust" if it.section == "food" else "General disgust",
        trait=it.subscale, image_query=_QUERIES.get(it.key, it.text),
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
        image_subject=_SUBSCALE_QUERY.get(r.biggest_ick_key, r.biggest_ick_label),
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
)
