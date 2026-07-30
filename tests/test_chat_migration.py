"""Re-keying chat-scoped data on a Telegram group→supergroup migration.

The DB is faked with an in-memory connection that models per-table chat_id
sets, so the transactional orchestration (create new parent → move every
chat_id table discovered from the catalog → drop old parent) is exercised
without Postgres.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.db.chat_migration import migrate_chat


class _FakeConn:
    def __init__(self, tables: list[str], data: dict[str, dict[int, int]]):
        self.tables = tables            # tables that have a chat_id column
        self.data = data               # table -> {chat_id: row_count}
        self.executed: list[str] = []

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Tx()

    async def execute(self, query: str, *args):
        self.executed.append(query)
        q = query.strip()
        if q.startswith("UPDATE") and "SET chat_id" in q:
            table = q.split()[1]
            new_id, old_id = args
            moved = self.data.get(table, {}).pop(old_id, 0)
            if moved:
                self.data[table][new_id] = moved
            return f"UPDATE {moved}"
        if q.startswith("DELETE FROM chats"):
            (old_id,) = args
            self.data.get("chats", {}).pop(old_id, None)
            return "DELETE 1"
        if q.startswith("INSERT INTO chats"):
            self.data.setdefault("chats", {})[args[0]] = 1
            return "INSERT 0 1"
        return "OK"

    async def fetch(self, query: str, *args):
        assert "information_schema.columns" in query
        return [{"table_name": t} for t in self.tables]


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acq()


def _fake_db(conn: _FakeConn):
    return SimpleNamespace(pool=_FakePool(conn))


OLD, NEW = -100, -1001234567890


@pytest.mark.asyncio
async def test_moves_every_chat_scoped_table():
    conn = _FakeConn(
        tables=["duck_stats", "chat_config", "messages", "quotes"],
        data={
            "duck_stats": {OLD: 5, -999: 3},     # another chat's rows must stay
            "chat_config": {OLD: 1},
            "messages": {OLD: 200},
            "quotes": {},                        # empty table → no move
            "chats": {OLD: 1},
        },
    )
    moved = await migrate_chat(_fake_db(conn), OLD, NEW)

    assert moved == {"duck_stats": 5, "chat_config": 1, "messages": 200}
    # Rows now live under the new id…
    assert conn.data["duck_stats"][NEW] == 5
    assert conn.data["messages"][NEW] == 200
    # …the unrelated chat is untouched…
    assert conn.data["duck_stats"][-999] == 3
    # …nothing left at the old id…
    assert OLD not in conn.data["duck_stats"]
    # …the new parent was created and the old parent deleted.
    assert any(q.strip().startswith("INSERT INTO chats") for q in conn.executed)
    assert OLD not in conn.data["chats"] and NEW in conn.data["chats"]


@pytest.mark.asyncio
async def test_noop_when_ids_equal():
    conn = _FakeConn(tables=["duck_stats"], data={"duck_stats": {OLD: 5}})
    moved = await migrate_chat(_fake_db(conn), OLD, OLD)
    assert moved == {}
    assert conn.executed == []               # never even opened a transaction


@pytest.mark.asyncio
async def test_idempotent_second_run_moves_nothing():
    conn = _FakeConn(
        tables=["duck_stats"],
        data={"duck_stats": {OLD: 5}, "chats": {OLD: 1}},
    )
    first = await migrate_chat(_fake_db(conn), OLD, NEW)
    second = await migrate_chat(_fake_db(conn), OLD, NEW)
    assert first == {"duck_stats": 5}
    assert second == {}                      # nothing left at OLD


@pytest.mark.asyncio
async def test_handler_extracts_old_and_new_ids(monkeypatch):
    """The basics handler pulls old=chat.id, new=migrate_to_chat_id and calls
    the re-keyer with them."""
    import ipedro.handlers.basics as basics

    called = {}

    async def _fake_migrate(db, old_id, new_id):
        called["args"] = (old_id, new_id)
        return {"duck_stats": 1}

    monkeypatch.setattr(basics, "migrate_chat", _fake_migrate)

    rt = SimpleNamespace(db=SimpleNamespace())
    router = basics.build_router(rt)
    handler = next(
        h.callback for h in router.observers["message"].handlers
        if h.callback.__name__ == "on_chat_migration"
    )
    msg = SimpleNamespace(
        chat=SimpleNamespace(id=OLD), migrate_to_chat_id=NEW,
    )
    await handler(msg)
    assert called["args"] == (OLD, NEW)


@pytest.mark.asyncio
async def test_handler_ignores_missing_or_equal_target(monkeypatch):
    import ipedro.handlers.basics as basics
    calls = []
    monkeypatch.setattr(
        basics, "migrate_chat",
        AsyncMock(side_effect=lambda *a: calls.append(a)),
    )
    rt = SimpleNamespace(db=SimpleNamespace())
    handler = next(
        h.callback for h in basics.build_router(rt).observers["message"].handlers
        if h.callback.__name__ == "on_chat_migration"
    )
    # migrate_to_chat_id equal to the current id → no-op.
    await handler(SimpleNamespace(chat=SimpleNamespace(id=OLD),
                                  migrate_to_chat_id=OLD))
    assert calls == []
