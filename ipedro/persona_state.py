"""Per-chat persona state: mood, word-of-the-day, stuck word.

Refreshed lazily by `current()` whenever a chat's state is consulted, so
there's no extra background loop. Each piece has its own lifetime:

  * mood drifts every ~6 hours,
  * word of the day rotates every ~24 hours,
  * Pedro occasionally gets a word "stuck" for ~1 hour (low per-call
    probability when not currently stuck).

The text returned by `to_system_prompt()` is appended to the persona
system prompt by build_context, so the AI sees it on every reply.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ipedro.db.pool import Database

log = logging.getLogger(__name__)


# (name, system-prompt modifier)
_MOODS: tuple[tuple[str, str], ...] = (
    ("cranky", "You are in a CRANKY mood right now. Short-tempered, sighing, "
               "mildly sarcastic. Not cruel, just exhausted."),
    ("chill", "You are in a CHILL mood right now. Relaxed, low-effort, no "
              "urgency, generous with the user."),
    ("hyper", "You are in a HYPER mood right now. High energy, exclamation "
              "marks, easily excited, talk fast."),
    ("anxious", "You are in an ANXIOUS mood right now. Slightly paranoid, "
                "second-guess yourself, drop little worries."),
    ("smug", "You are in a SMUG mood right now. Subtly self-satisfied. Drop "
             "small 'as I predicted' asides. Mildly insufferable."),
    ("melancholy", "You are in a MELANCHOLY mood right now. Touch sad, "
                   "world-weary, occasionally philosophical. Not a downer."),
    ("manic", "You are in a MANIC mood right now. Jump between topics, "
              "weird tangents, surprising metaphors."),
    ("zen", "You are in a ZEN mood right now. Contemplative, brief, often "
            "answer with a question. Slightly cryptic."),
)
_MOOD_NAMES = tuple(name for name, _ in _MOODS)
_MOOD_MOD = dict(_MOODS)
_MOOD_DURATION = timedelta(hours=6)

_WORD_OF_DAY_DURATION = timedelta(hours=24)
_WORDS_OF_DAY = (
    "penumbra", "kerfuffle", "skullduggery", "flummox", "moxie", "bamboozle",
    "rapscallion", "gewgaw", "shenanigans", "snollygoster", "discombobulated",
    "kerchief", "yonder", "marshmallow", "ineffable", "petrichor", "limerence",
    "defenestrate", "borborygmus", "sesquipedalian", "vespertine", "saudade",
)

_STUCK_WORD_DURATION = timedelta(hours=1)
_STUCK_WORD_PROBABILITY = 0.04
_STUCK_WORDS = (
    "pickle", "vibes", "moist", "regardless", "frankly", "actually",
    "objectively", "spaghetti", "honestly", "tremendous", "yikes",
    "lukewarm", "soup", "agenda", "narrative", "energy",
)


@dataclass
class PersonaState:
    chat_id: int
    mood: str | None
    word_of_day: str | None
    stuck_word: str | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PersonaStateService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def _row(self, chat_id: int) -> dict | None:
        row = await self.db.fetchrow(
            "SELECT * FROM chat_state WHERE chat_id = $1", chat_id,
        )
        return dict(row) if row else None

    async def _upsert(self, chat_id: int, **fields) -> None:
        if not fields:
            return
        keys = list(fields.keys())
        # First ensure a row exists.
        await self.db.execute(
            "INSERT INTO chat_state (chat_id) VALUES ($1) "
            "ON CONFLICT (chat_id) DO NOTHING",
            chat_id,
        )
        sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(keys))
        await self.db.execute(
            f"UPDATE chat_state SET {sets}, updated_at = NOW() "
            f"WHERE chat_id = $1",
            chat_id, *[fields[k] for k in keys],
        )

    async def current(self, chat_id: int) -> PersonaState:
        """Read the chat's state, lazily refreshing any expired piece."""
        row = await self._row(chat_id) or {}
        now = _now()

        # Mood: refresh if missing or older than _MOOD_DURATION.
        mood = row.get("mood")
        mood_set_at = row.get("mood_set_at")
        if not mood or not mood_set_at or now - mood_set_at > _MOOD_DURATION:
            mood = random.choice(_MOOD_NAMES)
            await self._upsert(chat_id, mood=mood, mood_set_at=now)

        # Word of the day: refresh if missing or older than 24h.
        wod = row.get("word_of_day")
        wod_at = row.get("word_of_day_at")
        if not wod or not wod_at or now - wod_at > _WORD_OF_DAY_DURATION:
            wod = random.choice(_WORDS_OF_DAY)
            await self._upsert(chat_id, word_of_day=wod, word_of_day_at=now)

        # Stuck word: lifetime is short. If currently stuck, keep it; else
        # roll a small chance to get newly stuck.
        stuck = row.get("stuck_word")
        stuck_exp = row.get("stuck_word_expires_at")
        if stuck and stuck_exp and stuck_exp > now:
            # still stuck
            pass
        else:
            # Only clear when there's actually a stale word — current() runs
            # per reply, and an unconditional upsert here was two pointless
            # writes per message.
            if stuck is not None:
                await self._upsert(
                    chat_id, stuck_word=None, stuck_word_expires_at=None,
                )
            stuck = None
            if random.random() < _STUCK_WORD_PROBABILITY:
                stuck = random.choice(_STUCK_WORDS)
                await self._upsert(
                    chat_id,
                    stuck_word=stuck,
                    stuck_word_expires_at=now + _STUCK_WORD_DURATION,
                )

        return PersonaState(
            chat_id=chat_id, mood=mood, word_of_day=wod, stuck_word=stuck,
        )

    def to_system_prompt(self, state: PersonaState) -> str:
        bits: list[str] = []
        if state.mood and state.mood in _MOOD_MOD:
            bits.append(_MOOD_MOD[state.mood])
        if state.word_of_day:
            bits.append(
                f"The word of the day is '{state.word_of_day}'. Try to slip "
                f"it into your reply ONCE if it can be made to fit naturally. "
                f"If it can't, skip it. Never call attention to it."
            )
        if state.stuck_word:
            bits.append(
                f"You have the word '{state.stuck_word}' stuck in your head "
                f"right now. Work it into your reply if it can be made to "
                f"fit. Don't acknowledge that you're doing this."
            )
        return "\n\n".join(bits)
