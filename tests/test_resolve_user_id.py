"""Tests for _resolve_user_id (admin user-reference resolver)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from ipedro.handlers.admin import _resolve_user_id


@dataclass
class FakeDB:
    """Replay a sequence of fetchrow / fetchval results and record calls."""

    fetchrow_results: list = field(default_factory=list)
    fetchval_results: list = field(default_factory=list)
    fetchrow_calls: list = field(default_factory=list)
    fetchval_calls: list = field(default_factory=list)

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if not self.fetchrow_results:
            return None
        return self.fetchrow_results.pop(0)

    async def fetchval(self, query, *args):
        self.fetchval_calls.append((query, args))
        if not self.fetchval_results:
            return 0
        return self.fetchval_results.pop(0)


@dataclass
class FakeRt:
    db: FakeDB


@pytest.mark.asyncio
async def test_numeric_passthrough_no_db():
    db = FakeDB()
    rt = FakeRt(db=db)
    assert await _resolve_user_id(rt, 100, "12345") == 12345
    assert await _resolve_user_id(rt, 100, "@12345") == 12345
    assert db.fetchrow_calls == []
    assert db.fetchval_calls == []


@pytest.mark.asyncio
async def test_username_match_case_insensitive():
    db = FakeDB(fetchrow_results=[{"user_id": 777}])
    rt = FakeRt(db=db)
    assert await _resolve_user_id(rt, 100, "BoB") == 777
    # First fetchrow call hit the users table.
    assert "FROM users" in db.fetchrow_calls[0][0]
    assert db.fetchrow_calls[0][1] == ("BoB",)


@pytest.mark.asyncio
async def test_username_no_match_falls_through_to_display_name():
    # First fetchrow (users) returns None, second (duck_stats) returns row.
    db = FakeDB(
        fetchrow_results=[None, {"user_id": 99}],
        fetchval_results=[1],
    )
    rt = FakeRt(db=db)
    assert await _resolve_user_id(rt, 42, "alice") == 99
    assert "FROM duck_stats" in db.fetchrow_calls[1][0]


@pytest.mark.asyncio
async def test_display_name_match_scoped_to_chat_id():
    db = FakeDB(
        fetchrow_results=[None, {"user_id": 11}],
        fetchval_results=[1],
    )
    rt = FakeRt(db=db)
    await _resolve_user_id(rt, 42, "alice")
    # Second fetchrow's args should be (chat_id, ref).
    second_args = db.fetchrow_calls[1][1]
    assert second_args[0] == 42
    assert second_args[1] == "alice"


@pytest.mark.asyncio
async def test_no_match_returns_none():
    db = FakeDB(
        fetchrow_results=[None, None],
        fetchval_results=[0],
    )
    rt = FakeRt(db=db)
    assert await _resolve_user_id(rt, 42, "nobody") is None


@pytest.mark.asyncio
async def test_priority_order_username_wins_over_display_name():
    # users row exists; second lookup must NOT happen.
    db = FakeDB(fetchrow_results=[{"user_id": 200}])
    rt = FakeRt(db=db)
    assert await _resolve_user_id(rt, 42, "alice") == 200
    assert len(db.fetchrow_calls) == 1


@pytest.mark.asyncio
async def test_leading_at_stripped_for_username_lookup():
    db = FakeDB(fetchrow_results=[{"user_id": 5}])
    rt = FakeRt(db=db)
    await _resolve_user_id(rt, 42, "@bob")
    assert db.fetchrow_calls[0][1] == ("bob",)


@pytest.mark.asyncio
async def test_double_at_treated_as_username_at_bob():
    db = FakeDB(fetchrow_results=[{"user_id": 5}])
    rt = FakeRt(db=db)
    await _resolve_user_id(rt, 42, "@@bob")
    # Only one leading '@' is stripped, so the lookup arg is "@bob".
    assert db.fetchrow_calls[0][1] == ("@bob",)


@pytest.mark.asyncio
async def test_partial_match_not_returned():
    # The SQL uses equality (LOWER(username) = LOWER($1)), so suffix
    # text isn't a partial match. We just verify the arg passes through verbatim.
    db = FakeDB(fetchrow_results=[None, None], fetchval_results=[0])
    rt = FakeRt(db=db)
    assert await _resolve_user_id(rt, 42, "bob@suffix") is None
    assert db.fetchrow_calls[0][1] == ("bob@suffix",)


@pytest.mark.asyncio
async def test_warns_on_multiple_display_name_matches(caplog):
    import logging
    db = FakeDB(
        fetchrow_results=[None, {"user_id": 1}],
        fetchval_results=[3],  # multiple matches
    )
    rt = FakeRt(db=db)
    with caplog.at_level(logging.WARNING, logger="ipedro.handlers.admin"):
        await _resolve_user_id(rt, 42, "alice")
    assert any("Multiple display_name matches" in r.message for r in caplog.records)
