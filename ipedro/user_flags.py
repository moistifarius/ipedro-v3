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

# Insult words, before or after one of the bot's names (or "bot") in the
# same message. The names come from the deployment's identity: this used
# to list only the legacy Dude/Pedro aliases, so "dale you're useless"
# never registered at all.
_INSULTS_BEFORE = (
    r"stupid|dumb|trash|garbage|shut\s*up|fuck\s*off|fuck\s*you|"
    r"shitty|hate|useless|broken|terrible|awful|kill\s*yourself|kys|"
    r"die|piece\s*of\s*shit"
)
_INSULTS_AFTER = (
    r"stupid|dumb|trash|garbage|shut\s*up|fuck\s*off|fuck\s*you|"
    r"shitty|hate|useless|broken|terrible|awful|piece\s*of\s*shit"
)


@lru_cache(maxsize=16)
def _insult_re(names_pattern: str) -> re.Pattern:
    # Lookarounds around the names, as in identity.py: an alias can end in
    # punctuation, where \b would never match.
    names = rf"(?<!\w)(?:(?:{names_pattern})|bot)(?!\w)"
    return re.compile(
        rf"\b(?:{_INSULTS_BEFORE})\b.{{0,40}}{names}"
        rf"|{names}.{{0,40}}\b(?:{_INSULTS_AFTER})\b",
        re.IGNORECASE,
    )


def is_insult_to_bot(
    text: str | None, names_pattern: str = DALE.names_pattern,
) -> bool:
    return bool(text) and _insult_re(names_pattern).search(text) is not None


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
    *, names_pattern: str = DALE.names_pattern,
) -> bool:
    """If `text` insults the bot, add a 24h grudge against user_id. Returns True if set."""
    if user_id is None or not is_insult_to_bot(text, names_pattern):
        return False
    await set_flag(db, chat_id, user_id, "grudge", ttl=GRUDGE_TTL)
    return True
