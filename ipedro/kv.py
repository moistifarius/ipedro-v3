"""Tiny key/value helper around the kv_store table."""

from __future__ import annotations

from ipedro.db.pool import Database


async def kv_get(db: Database, key: str) -> str | None:
    row = await db.fetchrow("SELECT value FROM kv_store WHERE key = $1", key)
    return row["value"] if row else None


async def kv_set(db: Database, key: str, value: str) -> None:
    await db.execute(
        """
        INSERT INTO kv_store (key, value) VALUES ($1, $2)
        ON CONFLICT (key) DO UPDATE
        SET value = EXCLUDED.value, updated_at = NOW()
        """,
        key, value,
    )


async def kv_delete(db: Database, key: str) -> bool:
    res = await db.execute("DELETE FROM kv_store WHERE key = $1", key)
    try:
        return int(res.split()[-1]) > 0
    except Exception:
        return False
