"""When the pool retries a statement, and when it must not.

asyncpg raises ConnectionDoesNotExistError when the socket drops WHILE a
statement is in flight, so the server may already have committed it. The
pool used to replay every statement on that error: +2 karma for one
reaction, a boss duck hit twice, a quote saved as #7 and #8.
"""

from __future__ import annotations

import asyncpg
import pytest

from ipedro.db.pool import Database, _is_read_only, _safe_to_retry

DEAD = asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed in the middle of operation")
CLOSED = asyncpg.exceptions.InterfaceError("connection is closed")


@pytest.mark.parametrize("query,read_only", [
    ("SELECT 1", True),
    ("  select * from messages where id = $1", True),
    ("WITH m AS (SELECT * FROM messages) SELECT count(*) FROM m", True),
    ("(SELECT 1)", True),
    ("INSERT INTO t VALUES ($1)", False),
    ("UPDATE t SET n = n + 1", False),
    ("DELETE FROM t", False),
    ("WITH p AS (INSERT INTO bot_posts (a) VALUES ($1) RETURNING id) "
     "SELECT pg_notify('bot_posts', id::text) FROM p", False),
    ("WITH x AS (UPDATE t SET n = 1 RETURNING n) SELECT * FROM x", False),
    ("SELECT nextval('s')", False),
    ("SELECT * FROM t WHERE id = $1 FOR UPDATE", False),
    ("SELECT pg_notify('c', 'x')", False),
    ("CREATE TABLE x (a int)", False),
    ("", False),
])
def test_what_counts_as_a_read(query, read_only):
    assert _is_read_only(query) is read_only


def test_a_connection_that_was_already_closed_is_always_safe_to_retry():
    """Raised before anything is sent, so nothing can be replayed."""
    assert _safe_to_retry(CLOSED, "UPDATE karma SET score = score + 1")


def test_a_mid_flight_loss_is_retried_for_reads_only():
    assert _safe_to_retry(DEAD, "SELECT 1")
    assert not _safe_to_retry(DEAD, "UPDATE karma SET score = score + 1")
    assert not _safe_to_retry(DEAD, "INSERT INTO quotes (text) VALUES ($1) RETURNING id")


class _Conn:
    def __init__(self, fail_first_with=None):
        self.calls = 0
        self.fail = fail_first_with

    async def execute(self, query, *args):
        self.calls += 1
        if self.fail is not None and self.calls == 1:
            raise self.fail
        return "UPDATE 1"

    fetch = fetchrow = fetchval = execute


class _Pool:
    def __init__(self, conn):
        self.conn = conn
        self.acquired = 0

    def acquire(self):
        outer = self

        class _A:
            async def __aenter__(self):
                outer.acquired += 1
                return outer.conn

            async def __aexit__(self, *e):
                return False

        return _A()


def _db(conn):
    return Database(_Pool(conn))


@pytest.mark.asyncio
async def test_a_write_is_not_replayed_after_a_mid_flight_loss():
    conn = _Conn(fail_first_with=DEAD)
    with pytest.raises(asyncpg.exceptions.ConnectionDoesNotExistError):
        await _db(conn).execute("UPDATE karma SET score = score + 1 WHERE user_id = $1", 7)
    assert conn.calls == 1                        # sent once, never twice


@pytest.mark.asyncio
async def test_a_read_is_retried_after_a_mid_flight_loss():
    conn = _Conn(fail_first_with=DEAD)
    assert await _db(conn).fetch("SELECT * FROM messages WHERE chat_id = $1", 1) == "UPDATE 1"
    assert conn.calls == 2


@pytest.mark.asyncio
async def test_a_write_is_retried_when_the_connection_was_closed_before_sending():
    conn = _Conn(fail_first_with=CLOSED)
    assert await _db(conn).execute("UPDATE karma SET score = score + 1") == "UPDATE 1"
    assert conn.calls == 2


@pytest.mark.asyncio
async def test_it_retries_once_not_forever():
    class _Always(_Conn):
        async def fetch(self, q, *a):
            self.calls += 1
            raise DEAD

    conn = _Always()
    with pytest.raises(asyncpg.exceptions.ConnectionDoesNotExistError):
        await _db(conn).fetch("SELECT 1")
    assert conn.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["execute", "fetch", "fetchrow", "fetchval"])
async def test_all_four_helpers_share_the_rule(helper):
    conn = _Conn(fail_first_with=DEAD)
    with pytest.raises(asyncpg.exceptions.ConnectionDoesNotExistError):
        await getattr(_db(conn), helper)("DELETE FROM t WHERE id = $1", 1)
    assert conn.calls == 1


# ── a numbered insert takes a per-key lock first, and is never replayed ──────

class _LockConn:
    def __init__(self, log, fail=False):
        self.log, self.fail = log, fail

    def transaction(self):
        log = self.log

        class _Tx:
            async def __aenter__(self_):
                log.append("begin")

            async def __aexit__(self_, *exc):
                log.append("end")
                return False
        return _Tx()

    async def execute(self, query, *args):
        self.log.append(("execute", query, args))

    async def fetchval(self, query, *args):
        self.log.append(("fetchval", query, args))
        if self.fail:
            raise DEAD
        return 7


class _LockPool:
    def __init__(self, conn):
        self.conn, self.acquired = conn, 0

    def acquire(self):
        pool = self

        class _Acq:
            async def __aenter__(self_):
                pool.acquired += 1
                return pool.conn

            async def __aexit__(self_, *exc):
                return False
        return _Acq()


@pytest.mark.asyncio
async def test_a_serialized_query_takes_its_lock_before_it_reads():
    log: list = []
    db = Database(_LockPool(_LockConn(log)))
    assert await db.fetchval_serialized(-100, "INSERT ...", 1, 2) == 7
    assert log[0] == "begin"
    assert log[1] == ("execute", "SELECT pg_advisory_xact_lock($1)", (-100,))
    assert log[2] == ("fetchval", "INSERT ...", (1, 2))      # a LATER statement
    assert log[3] == "end"


@pytest.mark.asyncio
async def test_a_serialized_write_is_not_replayed_when_the_connection_drops():
    pool = _LockPool(_LockConn([], fail=True))
    db = Database(pool)
    with pytest.raises(asyncpg.exceptions.ConnectionDoesNotExistError):
        await db.fetchval_serialized(-100, "INSERT ...", 1)
    assert pool.acquired == 1
