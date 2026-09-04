"""Data model for the quiz engine.

A quiz is a short self-report instrument: an ordered item bank rated on one
Likert scale, a pure `score(answers) -> QuizResult` function, and a bit of
presentation metadata. The engine (see `engine.py`) owns everything else —
the interactive flow, per-item images, the result card, storage, the
leaderboard — so adding a new quiz is just data + a scoring function.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class QuizItem:
    key: str            # stable id (used as the image-cache key)
    emoji: str
    text: str           # the statement/scenario shown to the taker
    section: str = ""   # optional header label (e.g. "Food disgust")
    trait: str = ""     # which subscale/trait this item loads on
    reverse: bool = False   # reverse-keyed (high rating = low trait)
    image_query: str = ""   # web-search term for its illustration (falls back to text)


@dataclass(frozen=True)
class QuizResult:
    headline: float                          # ranking axis, in scale units
    headline_max: float
    summary: str                             # short type/band label
    meters: list[tuple[str, float, str]]     # (label, fraction 0..1, value text)
    extras: list[str]                        # extra one-liner result lines
    image_subject: str                       # phrase for the result illustration
    verdict_payload: str                     # description fed to the persona verdict
    detail: dict = field(default_factory=dict)   # stored as JSONB


@dataclass(frozen=True)
class Quiz:
    id: str                      # slug, also the callback + storage key
    title: str
    emoji: str
    commands: tuple[str, ...]    # slash commands that start it
    board_commands: tuple[str, ...]   # slash commands for its leaderboard ('' → none)
    blurb: str                   # one-liner for the /tests menu
    prompt_lead: str             # lead-in above each item ("How much do you agree…")
    scale_min: int
    scale_max: int
    scale_legend: str            # e.g. "1 = strongly disagree · 7 = strongly agree"
    scale_words: dict[int, str]  # per-point word, for the tap-feedback toast
    items: tuple[QuizItem, ...]
    citation: str                # short "based on …" line
    verdict_instruction: str     # system instruction shaping the persona verdict
    score: Callable[[list[int]], QuizResult]

    @property
    def n_items(self) -> int:
        return len(self.items)


def reverse_value(quiz: Quiz, value: int) -> int:
    """Flip a rating on a reverse-keyed item (1↔max, 2↔max-1, …)."""
    return quiz.scale_min + quiz.scale_max - value


def agreement_words(scale_max: int) -> dict[int, str]:
    """Tap-feedback words for a 1..scale_max agreement scale."""
    words = {
        1: "strongly disagree", 2: "disagree", 3: "slightly disagree",
        4: "neutral", 5: "slightly agree", 6: "agree", 7: "strongly agree",
    }
    return {k: v for k, v in words.items() if k <= scale_max}
