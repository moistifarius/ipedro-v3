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
                    # Raise so the transaction rolls back. Continuing here
                    # would reach step 3, whose CASCADE delete of the old
                    # parent DESTROYS the skipped table's rows.
                    raise RuntimeError(
                        f"chat migration: refusing odd table name {table!r}"
                    )
                # Rows whose PRIMARY KEY / UNIQUE identity already exists
                # under the new id can't be moved (e.g. a message recorded
                # under the supergroup id before this service message was
                # processed). Skip exactly those rows — the new-id data
                # wins — instead of letting one collision abort the whole
                # migration and orphan everything.
                collision = await _collision_predicate(conn, table)
                if collision:
                    status = await conn.execute(
                        f"UPDATE {table} SET chat_id = $1 "
                        f" WHERE chat_id = $2 AND NOT ({collision})",
                        new_id, old_id,
                    )
                else:
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
                # Anything still at the old id collided; drop it (it's a
                # duplicate of data the new id already has) and report it.
                left = await conn.execute(
                    f"DELETE FROM {table} WHERE chat_id = $1", old_id,
                )
                try:
                    dropped = int(left.split()[-1])
                except (ValueError, IndexError):
                    dropped = 0
                if dropped:
                    log.warning(
                        "chat migration %s -> %s: dropped %d colliding %s row(s)",
                        old_id, new_id, dropped, table,
                    )
                    moved[f"{table} (dropped duplicates)"] = dropped

            # Merged quote books can hold duplicate per-chat seq numbers
            # (both chats had their own #1, #2, …), which would make
            # /unquote #N delete several rows. Renumber once, by original id.
            await conn.execute(
                """
                UPDATE quotes q
                   SET seq = n.rn
                  FROM (SELECT id,
                               ROW_NUMBER() OVER (ORDER BY id) AS rn
                          FROM quotes WHERE chat_id = $1) n
                 WHERE q.id = n.id AND q.chat_id = $1
                """,
                new_id,
            )

            # 3. Remove the old parent; all its children have moved.
            await conn.execute("DELETE FROM chats WHERE chat_id = $1", old_id)

    return moved


async def _collision_predicate(conn, table: str) -> str | None:
    """SQL predicate marking old-id rows whose identity already exists at the
    new id ($1), built from every PK/UNIQUE constraint that includes chat_id.

    Returns None when the table has no such constraint (nothing can collide).
    Column names come from our own catalog and are validated before being
    formatted into SQL.
    """
    rows = await conn.fetch(
        """
        SELECT tc.constraint_name,
               array_agg(kcu.column_name::text
                         ORDER BY kcu.ordinal_position) AS cols
          FROM information_schema.table_constraints tc
          JOIN information_schema.key_column_usage kcu
            ON kcu.constraint_name = tc.constraint_name
           AND kcu.table_schema = tc.table_schema
         WHERE tc.table_schema = 'public'
           AND tc.table_name = $1
           AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
         GROUP BY tc.constraint_name
        """,
        table,
    )
    predicates: list[str] = []
    for r in rows:
        cols = list(r["cols"])
        if "chat_id" not in cols:
            continue
        others = [c for c in cols if c != "chat_id"]
        if any(not _SAFE_IDENT.match(c) for c in others):
            raise RuntimeError(
                f"chat migration: odd column name in {table!r} constraint"
            )
        if others:
            match = " AND ".join(f"t2.{c} = {table}.{c}" for c in others)
        else:
            # Constraint is chat_id alone (e.g. chat_config PK): ANY row
            # already at the new id is a collision.
            match = "TRUE"
        predicates.append(
            f"EXISTS (SELECT 1 FROM {table} t2 "
            f"WHERE t2.chat_id = $1 AND {match})"
        )
    return " OR ".join(predicates) if predicates else None
