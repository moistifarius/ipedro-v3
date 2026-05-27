"""Asyncpg connection pool wrapper with optional pgvector support."""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

_VECTOR_REGISTERED: set[int] = set()


async def _register_vector(conn: asyncpg.Connection) -> None:
    """Best-effort pgvector codec registration. Safe if extension is missing."""
    try:
        from pgvector.asyncpg import register_vector  # type: ignore

        await register_vector(conn)
        _VECTOR_REGISTERED.add(id(conn))
    except Exception as exc:  # pragma: no cover - depends on installed extension
        log.debug("pgvector not registered on this connection: %s", exc)


class Database:
    """Thin wrapper around an asyncpg pool with helpers and vector codec setup."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool

    @classmethod
    async def connect(cls, dsn: str, min_size: int = 1, max_size: int = 10) -> "Database":
        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=min_size,
            max_size=max_size,
            init=_register_vector,
        )
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def execute(self, query: str, *args: Any) -> str:
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(query, *args)


_db_instance: Database | None = None


def set_db(db: Database) -> None:
    global _db_instance
    _db_instance = db


def get_db() -> Database:
    if _db_instance is None:
        raise RuntimeError("Database not initialised - call set_db() first")
    return _db_instance
