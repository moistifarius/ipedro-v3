"""Admin-only override: silence specific chats from the bot's ambient loops.

A small set of chat_ids that the admin has flagged as "be quiet here".
Stored in ``kv_store`` so the state survives restarts; mirrored into an
in-memory set so the hot path (every ambient-loop send) is O(1) and
synchronous.

Only the loop-driven big-event messages consult this — the confession
surfacer, the yearly retrospective, the daily fortune, and the
celebrations loop. Everything else is already sent with
``disable_notification=True`` and unaffected.

Not exposed via ``/chat_config`` or the DM config wizard on purpose: this
is the admin's lever, not a chat-member-facing toggle.
"""

from __future__ import annotations

from ipedro.db.pool import Database
from ipedro.kv import kv_delete, kv_get, kv_set

# kv_store key. Single CSV row, not one row per chat — the set is small
# and we always need the whole thing.
_KEY = "silenced_chats"

_cache: set[int] = set()


async def load_all(db: Database) -> None:
    """Prime the in-memory set from kv_store at startup.

    Empty/missing row is treated as "nothing silenced". Idempotent.
    """
    raw = await kv_get(db, _KEY)
    _cache.clear()
    if not raw:
        return
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            _cache.add(int(tok))
        except ValueError:
            continue


def is_silenced(chat_id: int) -> bool:
    """Synchronous, hot-path check. Defaults to False."""
    return chat_id in _cache


def list_silenced() -> list[int]:
    """Return the silenced chat_ids, sorted, for display."""
    return sorted(_cache)


async def silence(db: Database, chat_id: int) -> bool:
    """Mark a chat silenced. Returns True if newly added, False if already on."""
    if chat_id in _cache:
        return False
    _cache.add(chat_id)
    await _persist(db)
    return True


async def unsilence(db: Database, chat_id: int) -> bool:
    """Drop a chat from the silenced set. Returns True if it was on."""
    if chat_id not in _cache:
        return False
    _cache.discard(chat_id)
    await _persist(db)
    return True


async def _persist(db: Database) -> None:
    if _cache:
        await kv_set(db, _KEY, ",".join(str(c) for c in sorted(_cache)))
    else:
        await kv_delete(db, _KEY)


def _reset_cache_for_tests() -> None:
    _cache.clear()
