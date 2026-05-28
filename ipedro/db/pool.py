"""Asyncpg connection pool wrapper with optional pgvector support.

The pool keeps a handful of connections open across the bot's lifetime.
Long idle stretches (no chat activity, no background tasks) regularly
hit two failure modes:

  * Postgres / Docker bridge networking silently kills connections that
    sit idle longer than its TCP keepalive window.
  * asyncpg recycles idle connections itself, but there's a race: a
    connection can be released to the pool, killed server-side, then
    handed back out to the next caller before asyncpg notices — that
    caller sees `ConnectionDoesNotExistError`.

We mitigate both:

  * `max_inactive_connection_lifetime=60` recycles idle connections
    much faster than the default 300s, shrinking the race window.
  * Every helper (`execute`, `fetch`, `fetchrow`, `fetchval`) retries
    once on `ConnectionDoesNotExistError` / `InterfaceError`. The
    failed connection gets discarded by asyncpg and the retry acquires
    a fresh one.

This trades a tiny bit of connection churn for resilience against the
"bot stops responding after sitting idle" symptom.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

_VECTOR_REGISTERED: set[int] = set()

# asyncpg exceptions that indicate the connection we got from the pool was
# already dead. Always safe to retry once — we just drop the dead conn and
# grab a fresh one.
_STALE_CONN_ERRORS: tuple[type[BaseException], ...] = (
    asyncpg.exceptions.ConnectionDoesNotExistError,
    asyncpg.exceptions.InterfaceError,
)


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
            max_inactive_connection_lifetime=60,
            command_timeout=30,
        )
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def execute(self, query: str, *args: Any) -> str:
        for attempt in range(2):
            try:
                async with self._pool.acquire() as conn:
                    return await conn.execute(query, *args)
            except _STALE_CONN_ERRORS as exc:
                if attempt == 0:
                    log.warning("DB execute hit stale connection, retrying: %s", exc)
                    continue
                raise
        raise RuntimeError("unreachable")

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        for attempt in range(2):
            try:
                async with self._pool.acquire() as conn:
                    return await conn.fetch(query, *args)
            except _STALE_CONN_ERRORS as exc:
                if attempt == 0:
                    log.warning("DB fetch hit stale connection, retrying: %s", exc)
                    continue
                raise
        raise RuntimeError("unreachable")

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        for attempt in range(2):
            try:
                async with self._pool.acquire() as conn:
                    return await conn.fetchrow(query, *args)
            except _STALE_CONN_ERRORS as exc:
                if attempt == 0:
                    log.warning("DB fetchrow hit stale connection, retrying: %s", exc)
                    continue
                raise
        raise RuntimeError("unreachable")

    async def fetchval(self, query: str, *args: Any) -> Any:
        for attempt in range(2):
            try:
                async with self._pool.acquire() as conn:
                    return await conn.fetchval(query, *args)
            except _STALE_CONN_ERRORS as exc:
                if attempt == 0:
                    log.warning("DB fetchval hit stale connection, retrying: %s", exc)
                    continue
                raise
        raise RuntimeError("unreachable")


_db_instance: Database | None = None


def set_db(db: Database) -> None:
    global _db_instance
    _db_instance = db


def get_db() -> Database:
    if _db_instance is None:
        raise RuntimeError("Database not initialised - call set_db() first")
    return _db_instance
