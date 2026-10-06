"""Per-(chat, user) moderation flags: shutup, snark, grudge."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from ipedro.db.pool import Database
from ipedro.identity import DALE

log = logging.getLogger(__name__)

VALID_FLAGS = ("shutup", "snark", "grudge")

# Auto-grudge decays after this long. Re-insulting refreshes it.
GRUDGE_TTL = timedelta(hours=24)

# An insult has to be POINTED AT the bot: an insult word right next to one
# of its names (or "bot"), or the name followed by "you're / is / are" and
# the insult. Not merely both in the same message. The first version joined
# any insult word to any name within 40 characters, which is how "my car is
# broken dude", "the hinge is rusty and broken" and "dale cooper is trash"
# earned a 24-hour grudge: the bot turned snarky at someone who'd said
# nothing to it. Words that are as often a complaint about something else
# ("broken", "hate", "die") aren't in the list at all.
_ADJ = (
    r"(?:stupid|dumb|trash|garbage|useless|shitty|terrible|awful|worthless"
    r"|pathetic)"
)
_CMD = (
    r"(?:shut\s*up|stfu|fuck\s*off|fuck\s*you|fuck\s*u|kys"
    r"|kill\s*yourself|piece\s*of\s*shit)"
)
# Words that can sit between the insult and the name without breaking the
# aim: "you stupid bot", "such a useless bot", "shut up you fucking dale".
_FILLER = (
    r"(?:you(?:'re|\s+are)?|u(?:\s*r)?|ur|so|such\s+an?|an?|the|total|complete"
    r"|absolute|fucking|f+ing|goddamn|damn|fkn)"
)
_COPULA = r"(?:you(?:'re|\s+are)?|ur|u\s*r|is|are)"


@lru_cache(maxsize=16)
def _insult_re(names_pattern: str, bot_word: bool = True) -> re.Pattern:
    # Lookarounds around the names, as in identity.py: an alias can end in
    # punctuation, where \b would never match. The generic word "bot" counts
    # as a name only for a bot that answers to it (identity.answers_to_bot_word).
    name = rf"(?<!\w)(?:(?:{names_pattern}){'|bot' if bot_word else ''})(?!\w)"
    # Dale's legacy "dude" / "duder" are also what people call each other:
    # "this movie is trash dude" is about the movie. So an insult right
    # BEFORE the name doesn't count when the name is just that word (the
    # name-first forms below still do: "dude you're garbage").
    vocative = r"(?!(?:dude|duder)(?!\w))" if names_pattern == DALE.names_pattern else ""
    before_name = rf"{vocative}{name}"
    return re.compile(
        # "useless bot", "shut up dale", "you stupid bot", "fuck you rusty"
        rf"\b(?:{_ADJ}|{_CMD})\b(?:\W+{_FILLER}){{0,3}}\W+{before_name}"
        # "dale you're useless", "bot is stupid", "rusty is so dumb"
        rf"|{name}\W+{_COPULA}\W+(?:{_FILLER}\W+){{0,2}}(?:{_ADJ}|piece\s*of\s*shit)\b"
        # "dale shut up", "bot fuck off"
        rf"|{name}\W+{_CMD}\b",
        re.IGNORECASE,
    )


def is_insult_to_bot(
    text: str | None, names_pattern: str = DALE.names_pattern,
    bot_word: bool = True,
) -> bool:
    return bool(text) and _insult_re(names_pattern, bot_word).search(text) is not None


async def set_flag(
    db: Database, chat_id: int, user_id: int, flag: str, *,
    ttl: timedelta | None = None, note: str | None = None,
) -> None:
    if flag not in VALID_FLAGS:
        return
    expires = datetime.now(timezone.utc) + ttl if ttl else None
    await db.execute(
        """
        INSERT INTO user_flags (chat_id, user_id, flag, expires_at, note)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (chat_id, user_id, flag) DO UPDATE SET
            expires_at = EXCLUDED.expires_at,
            note = EXCLUDED.note
        """,
        chat_id, user_id, flag, expires, note,
    )


async def clear_flag(
    db: Database, chat_id: int, user_id: int, flag: str,
) -> bool:
    res = await db.execute(
        "DELETE FROM user_flags WHERE chat_id = $1 AND user_id = $2 AND flag = $3",
        chat_id, user_id, flag,
    )
    try:
        return int(res.split()[-1]) > 0
    except Exception:
        return False


async def has_flag(
    db: Database, chat_id: int, user_id: int | None, flag: str,
) -> bool:
    if user_id is None:
        return False
    row = await db.fetchrow(
        "SELECT 1 FROM user_flags "
        "WHERE chat_id = $1 AND user_id = $2 AND flag = $3 "
        "  AND (expires_at IS NULL OR expires_at > NOW())",
        chat_id, user_id, flag,
    )
    return row is not None


async def list_flags(db: Database, chat_id: int) -> list[dict]:
    rows = await db.fetch(
        "SELECT user_id, flag, expires_at, note FROM user_flags "
        "WHERE chat_id = $1 "
        "  AND (expires_at IS NULL OR expires_at > NOW()) "
        "ORDER BY flag, user_id",
        chat_id,
    )
    return [dict(r) for r in rows]


async def maybe_auto_grudge(
    db: Database, chat_id: int, user_id: int | None, text: str | None,
    *, names_pattern: str = DALE.names_pattern, bot_word: bool = True,
) -> bool:
    """If `text` insults the bot, add a 24h grudge against user_id. Returns True if set."""
    if user_id is None or not is_insult_to_bot(text, names_pattern, bot_word):
        return False
    await set_flag(db, chat_id, user_id, "grudge", ttl=GRUDGE_TTL)
    return True
