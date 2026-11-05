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
    once when the connection turns out to be dead. The failed connection
    gets discarded by asyncpg and the retry acquires a fresh one. See
    `_safe_to_retry` for exactly when a retry is allowed: a statement that
    may already have run is not replayed unless it's a plain read.

This trades a tiny bit of connection churn for resilience against the
"bot stops responding after sitting idle" symptom.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import asyncpg

log = logging.getLogger(__name__)


# asyncpg exceptions that indicate the connection we got from the pool was
# dead. Whether retrying is SAFE depends on which one and on the statement:
# see _safe_to_retry.
_STALE_CONN_ERRORS: tuple[type[BaseException], ...] = (
    asyncpg.exceptions.ConnectionDoesNotExistError,
    asyncpg.exceptions.InterfaceError,
)

_WRITE_KEYWORD_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|CALL|DO|CREATE|ALTER|DROP|TRUNCATE|"
    r"NEXTVAL|SETVAL|PG_NOTIFY)\b",
    re.IGNORECASE,
)


def _is_read_only(query: str) -> bool:
    """A plain SELECT (or a WITH that only selects): running it twice is
    the same as running it once."""
    q = query.lstrip().lstrip("(").lstrip()
    head = q[:6].upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        return False
    return not _WRITE_KEYWORD_RE.search(q) and " FOR UPDATE" not in q.upper()


def _safe_to_retry(exc: BaseException, query: str) -> bool:
    """May this statement be sent again after `exc`?

    InterfaceError ("connection is closed", "pool is closing") is raised
    BEFORE anything is sent, so a retry can't replay anything.
    ConnectionDoesNotExistError is raised when the socket drops WHILE the
    statement is in flight, and the server may already have committed it
    (helpers autocommit). Replaying a write then applied it twice: karma
    +2 for one reaction, a boss duck hit twice, a quote saved as #7 and #8,
    a usage row counted twice. Reads can't be damaged, so those still retry;
    a write that loses its connection mid-flight raises instead.
    """
    if isinstance(exc, asyncpg.exceptions.InterfaceError):
        return True
    return _is_read_only(query)


async def _register_vector(conn: asyncpg.Connection) -> None:
    """Best-effort pgvector codec registration. Safe if extension is missing."""
    try:
        from pgvector.asyncpg import register_vector  # type: ignore

        await register_vector(conn)
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

    async def _run(self, op: str, query: str, args: tuple) -> Any:
        for attempt in range(2):
            try:
                async with self._pool.acquire() as conn:
                    return await getattr(conn, op)(query, *args)
            except _STALE_CONN_ERRORS as exc:
                if attempt == 0 and _safe_to_retry(exc, query):
                    log.warning("DB %s hit stale connection, retrying: %s", op, exc)
                    continue
                raise
        raise RuntimeError("unreachable")

    async def execute(self, query: str, *args: Any) -> str:
        return await self._run("execute", query, args)

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        return await self._run("fetch", query, args)

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        return await self._run("fetchrow", query, args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        return await self._run("fetchval", query, args)

    async def fetchval_serialized(self, lock_key: int, query: str, *args: Any) -> Any:
        """`fetchval` under a per-key advisory lock, in its own transaction.

        For "read the state, then write the next number" statements
        (MAX(seq)+1): two of them running together both read the same
        state and both wrote the same number. The lock is taken in an
        earlier statement than the query, so the query's snapshot already
        includes whatever the previous holder committed. Not retried: it
        is a write, and a retry after a lost connection could double it."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", int(lock_key))
                return await conn.fetchval(query, *args)


_db_instance: Database | None = None


def set_db(db: Database) -> None:
    global _db_instance
    _db_instance = db


def get_db() -> Database:
    if _db_instance is None:
        raise RuntimeError("Database not initialised - call set_db() first")
    return _db_instance
