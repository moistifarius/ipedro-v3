"""The set of available quizzes, in menu order."""

from __future__ import annotations

from ipedro.quizzes.bigfive import BIG_FIVE
from ipedro.quizzes.darktriad import DARK_TRIAD
from ipedro.quizzes.disgust import DISGUST
from ipedro.quizzes.neophobia import NEOPHOBIA
from ipedro.quizzes.types import Quiz

# Order here is the order they appear in the /tests menu.
_QUIZZES: tuple[Quiz, ...] = (DISGUST, DARK_TRIAD, NEOPHOBIA, BIG_FIVE)
_BY_ID: dict[str, Quiz] = {q.id: q for q in _QUIZZES}


def all_quizzes() -> list[Quiz]:
    return list(_QUIZZES)


def get(quiz_id: str) -> Quiz | None:
    return _BY_ID.get(quiz_id)
