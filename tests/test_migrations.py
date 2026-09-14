"""apply_schema's pgvector-unavailable fallback.

The fallback strips the ivfflat index DDL by an exact string match against
schema.sql's current formatting. If that string ever drifts and the match
silently misses, the retried schema would still reference an ivfflat index
over what the same fallback just rewrote to a plain TEXT column — a
confusing failure two steps removed from its real cause. The fallback must
fail loudly at the point of the miss instead.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.db.migrations import apply_schema


def _db(*, first_call_raises: Exception | None):
    """A fake DB whose FIRST execute() (the full schema, with pgvector)
    can be made to fail; every later execute() (the retry, the version
    insert) succeeds."""
    calls: list[str] = []

    async def execute(sql, *args):
        calls.append(sql)
        if len(calls) == 1 and first_call_raises is not None:
            raise first_call_raises
        return "OK"

    return SimpleNamespace(execute=AsyncMock(side_effect=execute), calls=calls)


@pytest.mark.asyncio
async def test_pgvector_available_applies_the_schema_once_unmodified():
    db = _db(first_call_raises=None)
    await apply_schema(db, embedding_dim=1536)
    # Full schema, then the version-stamp insert — no fallback retry.
    assert db.execute.await_count == 2
    assert "vector(1536)" in db.calls[0]


@pytest.mark.asyncio
async def test_missing_pgvector_extension_falls_back_and_strips_ivfflat():
    err = RuntimeError('extension "vector" is not available')
    db = _db(first_call_raises=err)
    await apply_schema(db, embedding_dim=1536)
    # First call: the real schema (fails). Second: the stripped fallback.
    # Third: the version-stamp insert.
    assert db.execute.await_count == 3
    fallback_sql = db.calls[1]
    assert "vector(1536)" not in fallback_sql
    assert "USING ivfflat" not in fallback_sql     # the CREATE INDEX itself
    assert "pgvector unavailable: semantic index disabled" in fallback_sql
    assert "TEXT" in fallback_sql


@pytest.mark.asyncio
async def test_a_non_pgvector_failure_is_never_silently_retried():
    """A SQL typo, a lock timeout, a permissions error — none of these
    look like a missing-pgvector problem, so retrying a stripped schema
    would just fail again with a misleading message. Must surface as
    itself."""
    err = RuntimeError("syntax error at or near 'FOOBAR'")
    db = _db(first_call_raises=err)
    with pytest.raises(RuntimeError, match="syntax error"):
        await apply_schema(db, embedding_dim=1536)
    assert db.execute.await_count == 1          # never even tried a retry


@pytest.mark.asyncio
async def test_a_drifted_ivfflat_line_fails_loudly_instead_of_silently(monkeypatch):
    """If schema.sql's ivfflat DDL formatting ever changes, the exact
    string match in the fallback misses — it must not silently execute a
    schema that still references an index type the just-rewritten TEXT
    column can't support."""
    import ipedro.db.migrations as migrations

    monkeypatch.setattr(
        migrations, "_load_schema_sql",
        lambda embedding_dim: (
            "CREATE TABLE embeddings (embedding vector(1536));\n"
            "CREATE INDEX IF NOT EXISTS embeddings_vec_idx\n"
            "    ON embeddings USING ivfflat (embedding vector_cosine_ops) "
            "WITH (lists = 200);\n"   # <- drifted: 200, not 100
        ),
    )
    err = RuntimeError('extension "vector" is not available')
    db = _db(first_call_raises=err)
    with pytest.raises(RuntimeError, match="could not find the ivfflat"):
        await apply_schema(db, embedding_dim=1536)
