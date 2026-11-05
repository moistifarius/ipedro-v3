"""The admin's silenced-chat set: in memory for the hot path, in kv_store to
survive a restart. The two must not disagree when a write fails."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ipedro import silenced_chats as sc


@pytest.fixture(autouse=True)
def _clean():
    sc._reset_cache_for_tests()
    yield
    sc._reset_cache_for_tests()


@pytest.fixture
def kv(monkeypatch):
    store = {"set": AsyncMock(), "delete": AsyncMock()}
    monkeypatch.setattr(sc, "kv_set", store["set"])
    monkeypatch.setattr(sc, "kv_delete", store["delete"])
    return store


@pytest.mark.asyncio
async def test_silencing_and_unsilencing_persist(kv):
    assert await sc.silence(object(), -5) is True
    assert sc.is_silenced(-5)
    kv["set"].assert_awaited_once()
    assert await sc.silence(object(), -5) is False                # already on
    assert await sc.unsilence(object(), -5) is True
    assert not sc.is_silenced(-5)
    kv["delete"].assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_save_leaves_the_chat_not_silenced(kv):
    kv["set"].side_effect = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        await sc.silence(object(), -5)
    assert not sc.is_silenced(-5)                                  # nothing half-applied


@pytest.mark.asyncio
async def test_a_failed_unsilence_leaves_the_chat_silenced(kv):
    await sc.silence(object(), -5)
    kv["delete"].side_effect = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        await sc.unsilence(object(), -5)
    assert sc.is_silenced(-5)
