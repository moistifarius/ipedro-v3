"""Tests for MemoryStore.wipe_conversation — the persona-reset helper.

A new persona keeps leaking the old voice because the bot's past
assistant replies, the running summary, and the embeddings all feed
back into the context window. wipe_conversation clears that precedent.
We fake the DB to assert exactly which tables get cleared and that the
returned counts come from the asyncpg status strings.
"""

from __future__ import annotations

import pytest

from ipedro.memory.store import MemoryStore


class _FakeDB:
    """Records DELETEs and returns a configurable asyncpg-style status."""

    def __init__(self, counts: dict[str, int]):
        # counts keyed by table name; default 0 if absent.
        self._counts = counts
        self.deleted: list[tuple[str, int]] = []  # (table, chat_id)

    async def execute(self, query: str, *args) -> str:
        # Extract the table name from "DELETE FROM <table> WHERE ...".
        table = query.split("FROM", 1)[1].split()[0]
        chat_id = args[0]
        self.deleted.append((table, chat_id))
        return f"DELETE {self._counts.get(table, 0)}"


@pytest.mark.asyncio
async def test_wipe_clears_messages_summaries_embeddings_not_facts_by_default():
    db = _FakeDB({"messages": 42, "summaries": 3, "embeddings": 40})
    store = MemoryStore(db=db)  # type: ignore[arg-type]

    counts = await store.wipe_conversation(chat_id=99)

    tables = {t for t, _ in db.deleted}
    assert tables == {"messages", "summaries", "embeddings"}
    assert "facts" not in tables                      # kept by default
    assert all(cid == 99 for _, cid in db.deleted)    # scoped to the chat
    assert counts == {"messages": 42, "summaries": 3, "embeddings": 40}


@pytest.mark.asyncio
async def test_wipe_includes_facts_when_requested():
    db = _FakeDB({"messages": 5, "summaries": 1, "embeddings": 5, "facts": 7})
    store = MemoryStore(db=db)  # type: ignore[arg-type]

    counts = await store.wipe_conversation(chat_id=1, include_facts=True)

    assert ("facts", 1) in db.deleted
    assert counts["facts"] == 7


@pytest.mark.asyncio
async def test_wipe_on_empty_chat_returns_zeros():
    db = _FakeDB({})  # every DELETE reports 0 rows
    store = MemoryStore(db=db)  # type: ignore[arg-type]

    counts = await store.wipe_conversation(chat_id=7)

    assert counts == {"messages": 0, "summaries": 0, "embeddings": 0}


@pytest.mark.asyncio
async def test_wipe_tolerates_unparseable_status():
    class _WeirdDB:
        async def execute(self, query, *args):
            return "ok"  # not "DELETE N"

    store = MemoryStore(db=_WeirdDB())  # type: ignore[arg-type]
    counts = await store.wipe_conversation(chat_id=1)
    # Falls back to 0 rather than raising.
    assert counts == {"messages": 0, "summaries": 0, "embeddings": 0}
