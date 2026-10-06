"""The activity log is pruned: it gets a row per reply and per silence."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import housekeeping


@pytest.mark.asyncio
async def test_old_rows_go_in_batches_until_fewer_than_a_batch_remain(monkeypatch):
    monkeypatch.setattr(housekeeping, "_BATCH", 3)
    db = SimpleNamespace(execute=AsyncMock(side_effect=["DELETE 3", "DELETE 3", "DELETE 1"]))
    assert await housekeeping.prune_activity_log(db, 90) == 7
    assert db.execute.await_count == 3
    sql, days, batch = db.execute.await_args.args
    assert "FROM activity_log" in sql and "make_interval(days => $1)" in sql
    assert (days, batch) == (90, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [0, -5])
async def test_zero_days_keeps_everything(days):
    db = SimpleNamespace(execute=AsyncMock())
    assert await housekeeping.prune_activity_log(db, days) == 0
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_loop_prunes_and_survives_a_failure():
    stop = asyncio.Event()
    calls = []

    async def execute(sql, *args):
        calls.append(sql)
        stop.set()
        raise RuntimeError("db blip")

    db = SimpleNamespace(execute=execute)
    settings = SimpleNamespace(activity_retention_days=30)
    await asyncio.wait_for(housekeeping.run_housekeeping_loop(db, settings, stop), 2)
    assert len(calls) == 1                                  # tried, logged, carried on to stop


def test_the_retention_default_is_listed_and_sane():
    from ipedro.config import Settings

    s = Settings(telegram_bot_token="t", openai_api_key="k", database_url="postgresql://t/t")  # type: ignore[call-arg]
    assert s.activity_retention_days == 90
