"""Disgust personality test — item bank, scoring, and result rendering.

An *entertainment* self-report quiz adapted from published, validated disgust
instruments. It is deliberately short (16 items) and its scores are relative,
within-test bands only — it is NOT a clinical administration and claims no
diagnostic validity or population norms.

Two sections:

* **Food disgust** — the 8-item short form of the Food Disgust Scale, one item
  per validated subscale.
    Hartmann, C., & Siegrist, M. (2018). Development and validation of the Food
    Disgust Scale. Food Quality and Preference, 63, 38-50.
    Ammann, J., Hartmann, C., & Siegrist, M. (2019). Cross-cultural validation
    of the short version of the Food Disgust Scale in ten countries.
  (An image-based sibling exists — the Food Disgust Picture Scale; Ammann,
  Hartmann & Siegrist, 2018, Appetite, 125, 367-379 — which inspired pairing
  each item with a visual.)

* **General disgust sensitivity** — 8 items operationalising the three domains
  of the Disgust Scale-Revised.
    Olatunji, B. O., Williams, N. L., Tolin, D. F., Sawchuk, C. N., Abramowitz,
    J. S., Lohr, J. M., & Elwood, L. (2007). The Disgust Scale: Item analysis,
    factor structure, and suggestions for refinement. Psychological Assessment,
    19(3), 281-297.
    Haidt, J., McCauley, C., & Rozin, P. (1994). Individual differences in
    sensitivity to disgust. Personality and Individual Differences, 16, 701-713.

Every item is rated on the FDS 6-point scale (1 = not grossed out at all,
6 = extremely grossed out). Higher = more disgust-sensitive.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Response scale -------------------------------------------------------

SCALE_MIN = 1
SCALE_MAX = 6
SCALE_LEGEND = "1 = not grossed out  ·  6 = extremely grossed out"
# Short word per point, shown under the buttons.
SCALE_WORDS = {
    1: "not at all",
    2: "barely",
    3: "a little",
    4: "fairly",
    5: "very",
    6: "extremely",
}


@dataclass(frozen=True)
class Item:
    key: str          # stable id
    section: str      # "food" | "general"
    subscale: str     # subscale/domain key
    emoji: str
    text: str         # the scenario, phrased as "how grossed out would you be…"


# --- Section A: Food Disgust Scale short form (8 items) --------------------
# One item per FDS subscale (Hartmann & Siegrist, 2018).
FOOD_ITEMS: tuple[Item, ...] = (
    Item("cartilage", "food", "animal_flesh", "🦴",
         "putting a piece of chewy animal cartilage in your mouth"),
    Item("silverware", "food", "poor_hygiene", "🍴",
         "eating with visibly dirty silverware at a restaurant"),
    Item("shared_spoon", "food", "human_contamination", "🥄",
         "eating soup a stranger just tasted with your spoon"),
    Item("mould_cheese", "food", "mould", "🧀",
         "eating hard cheese that had a patch of mould cut off it"),
    Item("brown_apple", "food", "decaying_fruit", "🍎",
         "eating apple slices that turned brown after being cut"),
    Item("brown_avocado", "food", "decaying_vegetables", "🥑",
         "eating brown, mushy avocado pulp"),
    Item("fish_texture", "food", "fish", "🐟",
         "the slippery texture of a piece of fish in your mouth"),
    Item("salad_snail", "food", "living_contaminants", "🐌",
         "finding a little live snail in the salad you were about to eat"),
)

# --- Section B: Disgust Scale-Revised domains (8 items) --------------------
# 3 core, 3 animal-reminder, 2 contamination (Olatunji et al., 2007).
GENERAL_ITEMS: tuple[Item, ...] = (
    Item("maggots", "general", "core", "🪰",
         "seeing maggots on a piece of meat in an outdoor bin"),
    Item("bin_stench", "general", "core", "🗑️",
         "the stench of a rubbish bin baking in summer heat"),
    Item("vomit", "general", "core", "🤮",
         "watching someone throw up in front of you"),
    Item("hand_jar", "general", "animal_reminder", "🫙",
         "seeing a preserved human hand in a jar in a science class"),
    Item("dead_body", "general", "animal_reminder", "⚰️",
         "the thought of touching a dead body"),
    Item("glass_eye", "general", "animal_reminder", "👁️",
         "watching someone take out their glass eye"),
    Item("shared_glass", "general", "contamination", "🥤",
         "drinking from a glass a stranger drank from an hour ago"),
    Item("worn_shirt", "general", "contamination", "👕",
         "wearing a clean shirt a stranger had worn before you"),
)

ALL_ITEMS: tuple[Item, ...] = FOOD_ITEMS + GENERAL_ITEMS
N_ITEMS = len(ALL_ITEMS)  # 16

# Pretty labels for subscales/domains.
SUBSCALE_LABELS = {
    "animal_flesh": "animal flesh",
    "poor_hygiene": "poor hygiene",
    "human_contamination": "human contamination",
    "mould": "mould",
    "decaying_fruit": "decaying fruit",
    "decaying_vegetables": "decaying veg",
    "fish": "fish",
    "living_contaminants": "living things",
    "core": "core disgust",
    "animal_reminder": "animal-reminder",
    "contamination": "contamination",
}

CITATION_SHORT = (
    "Food Disgust Scale (Hartmann & Siegrist, 2018) + Disgust Scale-Revised "
    "(Olatunji et al., 2007). For fun, not a diagnosis."
)


def _mean(xs: list[int] | list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def progress_bar(position: int, total: int, width: int = 10) -> str:
    """A ▰▱ bar for 'you're on question `position` of `total`'."""
    filled = max(0, min(width, round(position / total * width)))
    return "▰" * filled + "▱" * (width - filled)


def meter(score: float, width: int = 10) -> str:
    """Render a 1-6 score as a ▰▱ bar."""
    frac = (score - SCALE_MIN) / (SCALE_MAX - SCALE_MIN)
    filled = max(0, min(width, round(frac * width)))
    return "▰" * filled + "▱" * (width - filled)


def item_image_prompt(item: "Item") -> str:
    """Prompt for a single question's cached illustration. Deliberately
    lighthearted and text/gore-free — image models render text poorly and the
    scenes must stay safe to generate and post."""
    return (
        "Funny lighthearted flat-vector cartoon sticker illustrating this "
        f"scene: {item.text}. Bold clean outlines, soft muted palette, a single "
        "clear subject centered on a plain background, no text, no words, no "
        "letters, not graphic, not gory, wholesome cartoon style."
    )


def band(score: float) -> str:
    """Map a 1-6 mean to a qualitative, non-diagnostic band label."""
    if score < 2.0:
        return "iron-stomached"
    if score < 3.0:
        return "pretty unbothered"
    if score < 4.0:
        return "middle of the road"
    if score < 5.0:
        return "squeamish"
    return "can barely cope"


BAND_EMOJI = {
    "iron-stomached": "🗿",
    "pretty unbothered": "😎",
    "middle of the road": "😐",
    "squeamish": "🫣",
    "can barely cope": "🤢",
}


@dataclass(frozen=True)
class DisgustResult:
    food_score: float
    general_score: float
    core_score: float
    animal_score: float
    contam_score: float
    overall_score: float
    biggest_ick_key: str        # subscale key of the strongest food reaction
    biggest_ick_label: str
    biggest_ick_emoji: str
    iron_stomach_label: str
    iron_stomach_emoji: str

    @property
    def food_band(self) -> str:
        return band(self.food_score)

    @property
    def general_band(self) -> str:
        return band(self.general_score)

    @property
    def overall_band(self) -> str:
        return band(self.overall_score)


def score(answers: list[int]) -> DisgustResult:
    """Turn 16 ordered answers (each 1-6) into a scored result.

    `answers` must be in ALL_ITEMS order: the 8 food items then the 8 general
    items. Raises ValueError on the wrong count or out-of-range values so a
    malformed session can't silently produce garbage scores.
    """
    if len(answers) != N_ITEMS:
        raise ValueError(f"expected {N_ITEMS} answers, got {len(answers)}")
    if any(a < SCALE_MIN or a > SCALE_MAX for a in answers):
        raise ValueError("answers must each be within 1-6")

    food = answers[:8]
    general = answers[8:]
    # Domain slices within the general section (3 core, 3 animal, 2 contam).
    core = general[0:3]
    animal = general[3:6]
    contam = general[6:8]

    # Biggest ick = strongest food reaction; ties resolve to the first item.
    max_food = max(food)
    min_food = min(food)
    ick_item = FOOD_ITEMS[food.index(max_food)]
    iron_item = FOOD_ITEMS[food.index(min_food)]

    return DisgustResult(
        food_score=round(_mean(food), 2),
        general_score=round(_mean(general), 2),
        core_score=round(_mean(core), 2),
        animal_score=round(_mean(animal), 2),
        contam_score=round(_mean(contam), 2),
        overall_score=round(_mean(answers), 2),
        biggest_ick_key=ick_item.subscale,
        biggest_ick_label=SUBSCALE_LABELS[ick_item.subscale],
        biggest_ick_emoji=ick_item.emoji,
        iron_stomach_label=SUBSCALE_LABELS[iron_item.subscale],
        iron_stomach_emoji=iron_item.emoji,
    )


def fallback_verdict(result: DisgustResult, name: str) -> str:
    """Deterministic verdict used when the AI persona call is unavailable."""
    b = result.overall_band
    if b == "iron-stomached":
        return (f"{name}, you've got the constitution of a bin lorry. "
                f"Nothing on this list moved the needle. Respect, and also concern.")
    if b == "pretty unbothered":
        return (f"{name} is hard to gross out. You'd eat the mystery leftovers "
                f"and not think twice.")
    if b == "middle of the road":
        return (f"{name}, you're a normal amount of grossed out. Textbook. "
                f"Boring, even. That's a compliment.")
    if b == "squeamish":
        return (f"{name} is squeamish and I respect the honesty. That {result.biggest_ick_label} "
                f"question clearly got you.")
    return (f"{name} nearly needed a lie-down doing this test. "
            f"Everything is an ick. Stay strong out there.")


def result_caption(
    result: DisgustResult, name: str, verdict: str,
    *, percentile: int | None = None,
) -> str:
    """The photo caption / text result. Kept under Telegram's 1024-char cap.

    `percentile`, when given, is the share of others in this chat whose overall
    score is lower than this taker's ("grosser than X%").
    """
    fe = BAND_EMOJI.get(result.food_band, "")
    ge = BAND_EMOJI.get(result.general_band, "")
    lines = [
        f"🧫 {name}'s Disgust Profile",
        "",
        f"🍽 Food     {meter(result.food_score)} {result.food_score}/6 · "
        f"{result.food_band} {fe}",
        f"🧠 General  {meter(result.general_score)} {result.general_score}/6 · "
        f"{result.general_band} {ge}",
        f"     core {meter(result.core_score, 6)} · "
        f"animal {meter(result.animal_score, 6)} · "
        f"contam {meter(result.contam_score, 6)}",
    ]
    if percentile is not None:
        lines.append(f"🩸 Grosser than {percentile}% of this chat")
    lines += [
        "",
        f"Biggest ick: {result.biggest_ick_label} {result.biggest_ick_emoji}   "
        f"Iron stomach: {result.iron_stomach_label} {result.iron_stomach_emoji}",
        "",
        verdict.strip(),
        "",
        f"📚 {CITATION_SHORT}",
    ]
    caption = "\n".join(lines)
    if len(caption) > 1024:
        # Trim the verdict rather than drop the citation/scores.
        overflow = len(caption) - 1024 + 1
        trimmed = verdict.strip()[: max(0, len(verdict.strip()) - overflow - 1)] + "…"
        try:
            lines[lines.index(verdict.strip())] = trimmed
        except ValueError:
            pass
        caption = "\n".join(lines)
    return caption[:1024]      # hard cap regardless of the trim above


def image_prompt(result: DisgustResult) -> str:
    """Prompt for the result illustration. Tasteful and text-free by design —
    image models render text poorly and we want no gore."""
    return (
        "A clean, cartoonish flat-vector 'lab report' badge illustration "
        f"representing a person who is '{result.overall_band}' about disgusting "
        f"things, with a playful motif of {result.biggest_ick_emoji} "
        f"({result.biggest_ick_label}). Muted mint-and-cream laboratory palette, "
        "a small specimen jar and clipboard, friendly and clinical, centered, "
        "no text, no words, no gore, sticker style."
    )
