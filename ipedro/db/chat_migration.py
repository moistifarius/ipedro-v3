"""Re-key chat-scoped data when Telegram migrates a group to a supergroup.

When a basic Telegram group is upgraded to a supergroup, Telegram assigns it a
brand-new ``chat_id`` and sends a service message carrying ``migrate_to_chat_id``.
Nothing in the bot used to react to that, so every chat-scoped row (duck stats,
config, memory, quotes, …) stayed keyed to the *old* id and silently orphaned —
the leaderboard, config, and history all appeared to reset to empty under the
new id. This module moves it all across in one transaction.

The foreign keys to ``chats(chat_id)`` are ``ON DELETE CASCADE`` with no
``ON UPDATE``, and several chat-scoped tables (duck_stats, bef_challenges, the
disgust-test tables) have no FK at all — so a plain ``UPDATE chats`` can't
cascade. Instead we: (1) create the new parent row, (2) move every table that
has a ``chat_id`` column (discovered from the catalog, so no present-or-future
table is missed), then (3) delete the now-childless old parent.
"""

from __future__ import annotations

import logging
import re

from ipedro.db.pool import Database

log = logging.getLogger(__name__)

# Postgres identifiers we're willing to interpolate into a query. Table names
# come from information_schema (our own catalog), but we still validate them
# before formatting into SQL — defence in depth, never trust a name into a string.
_SAFE_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


async def migrate_chat(db: Database, old_id: int, new_id: int) -> dict[str, int]:
    """Move every chat-scoped row from ``old_id`` to ``new_id``.

    Idempotent: running it again after the move is a no-op (nothing is left at
    the old id). Returns a map of table -> rows moved (tables with 0 moves
    omitted). Atomic — any failure rolls the whole thing back.
    """
    if new_id == old_id:
        return {}

    async with db.pool.acquire() as conn:
        async with conn.transaction():
            # 1. Ensure the new parent chats row exists so FK children can point
            #    at it. Copy the old row's title/first_seen when present.
            await conn.execute(
                "INSERT INTO chats (chat_id, type, title, first_seen, last_seen) "
                "SELECT $1, 'supergroup', title, first_seen, NOW() "
                "  FROM chats WHERE chat_id = $2 "
                "ON CONFLICT (chat_id) DO NOTHING",
                new_id, old_id,
            )
            # Guarantee the parent exists even if the old chats row was missing.
            await conn.execute(
                "INSERT INTO chats (chat_id, type) VALUES ($1, 'supergroup') "
                "ON CONFLICT (chat_id) DO NOTHING",
                new_id,
            )

            # 2. Move every table carrying a chat_id, discovered dynamically so
            #    a table added later can never be silently left behind.
            recs = await conn.fetch(
                "SELECT table_name FROM information_schema.columns "
                " WHERE table_schema = 'public' AND column_name = 'chat_id' "
                "   AND table_name <> 'chats' "
                " ORDER BY table_name"
            )
            moved: dict[str, int] = {}
            for rec in recs:
                table = rec["table_name"]
                if not _SAFE_IDENT.match(table):
                    log.warning("chat migration: skipping odd table name %r", table)
                    continue
                status = await conn.execute(
                    f"UPDATE {table} SET chat_id = $1 WHERE chat_id = $2",
                    new_id, old_id,
                )
                try:
                    n = int(status.split()[-1])
                except (ValueError, IndexError):
                    n = 0
                if n:
                    moved[table] = n

            # 3. Remove the old parent; all its children have moved.
            await conn.execute("DELETE FROM chats WHERE chat_id = $1", old_id)

    return moved
