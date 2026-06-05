"""Tests for the global /ducknames listing.

The DB is faked with a tiny in-memory stand-in so the test pins the SQL
contract (which rows are surfaced and which are filtered out) without
needing Postgres. The handler-level test also confirms pagination and
the empty-state reply.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.duckhunt.service import DuckhuntService


class _DucknamesFakeDB:
    """Minimal surface: list_named_ducks_global runs one COUNT(*) and
    one SELECT against duck_events ⋈ chat_config ⋈ users. We stub both
    queries from a single in-memory `rows` list, applying the same
    name-not-null / public-only / resolved-bef filter the SQL would.
    """

    def __init__(self, rows: list[dict]):
        self.rows = rows

    def _visible(self) -> list[dict]:
        return [
            r for r in self.rows
            if r.get("name") is not None
            and r.get("resolved") is True
            and r.get("resolved_action") == "bef"
            and r.get("duck_names_public", True) is True
        ]

    async def fetchval(self, query, *args):
        if "COUNT(*)" in query:
            return len(self._visible())
        return 0

    async def fetch(self, query, *args):
        limit, offset = args
        # newest-first → highest id first
        sliced = sorted(self._visible(), key=lambda r: -r["id"])
        page = sliced[offset:offset + limit]
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "resolved_by": r["resolved_by"],
                "owner": r.get("owner") or str(r["resolved_by"]),
            }
            for r in page
        ]


def _row(rid: int, name: str | None, owner: str = "alice", *,
         resolved: bool = True, action: str = "bef", public: bool = True,
         resolved_by: int = 1) -> dict:
    return {
        "id": rid, "name": name, "owner": owner,
        "resolved": resolved, "resolved_action": action,
        "duck_names_public": public, "resolved_by": resolved_by,
    }


@pytest.mark.asyncio
async def test_returns_only_named_befriended_ducks_from_public_chats():
    db = _DucknamesFakeDB([
        _row(1, "Whiskers"),
        _row(2, None),                          # unnamed → filtered
        _row(3, "Killer", action="bang"),       # not a bef → filtered
        _row(4, "Ghosted", resolved=False),     # still active → filtered
        _row(5, "Private", public=False),       # chat opted out → filtered
        _row(6, "The Don"),
    ])
    svc = DuckhuntService(db)  # type: ignore[arg-type]
    rows, total = await svc.list_named_ducks_global(limit=10)
    names = [r["name"] for r in rows]
    assert names == ["The Don", "Whiskers"]  # newest first by id
    assert total == 2


@pytest.mark.asyncio
async def test_pagination_and_count():
    rows_in = [_row(i, f"duck-{i}") for i in range(1, 11)]
    db = _DucknamesFakeDB(rows_in)
    svc = DuckhuntService(db)  # type: ignore[arg-type]

    page1, total = await svc.list_named_ducks_global(limit=3, offset=0)
    page2, _ = await svc.list_named_ducks_global(limit=3, offset=3)
    page_past_end, _ = await svc.list_named_ducks_global(limit=3, offset=99)

    assert total == 10
    assert len(page1) == 3 and len(page2) == 3
    assert page_past_end == []
    # No overlap.
    assert {r["name"] for r in page1} & {r["name"] for r in page2} == set()


@pytest.mark.asyncio
async def test_handler_empty_state(monkeypatch):
    """When no chat anywhere has a named duck, the reply explains
    instead of dumping an empty header."""
    from ipedro.handlers.duckhunt import build_router

    cfg = SimpleNamespace(duckhunt_enabled=True)
    chats = SimpleNamespace(
        upsert_chat=AsyncMock(),
        get_config=AsyncMock(return_value=cfg),
        upsert_default_config=AsyncMock(return_value=cfg),
    )
    users = SimpleNamespace(upsert_user=AsyncMock())
    duckhunt = SimpleNamespace(
        list_named_ducks_global=AsyncMock(return_value=([], 0)),
    )
    settings = SimpleNamespace(
        admin_ids=frozenset(),
        duckhunt_action_cooldown_seconds=15,
        duckhunt_duck_lifetime_seconds=86400,
    )
    rt = SimpleNamespace(
        settings=settings, db=SimpleNamespace(), chats=chats, users=users,
        duckhunt=duckhunt, openai=SimpleNamespace(), bot=SimpleNamespace(),
    )

    router = build_router(rt)
    handler = next(
        h.callback for h in router.observers["message"].handlers
        if h.callback.__name__ == "ducknames"
    )

    msg = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="group", title="t"),
        from_user=SimpleNamespace(id=7, is_bot=False, username="u",
                                  first_name="U", last_name=None),
        text="/ducknames", caption=None, message_id=1,
        reply=AsyncMock(),
    )
    await handler(msg)
    body = msg.reply.await_args.args[0]
    assert "/duckname" in body and "no named ducks" in body.lower()


@pytest.mark.asyncio
async def test_handler_paginates_and_shows_more_hint(monkeypatch):
    """When more pages exist, the reply includes a +N more hint pointing
    at the next page number."""
    from ipedro.handlers.duckhunt import build_router

    cfg = SimpleNamespace(duckhunt_enabled=True)
    chats = SimpleNamespace(
        upsert_chat=AsyncMock(),
        get_config=AsyncMock(return_value=cfg),
        upsert_default_config=AsyncMock(return_value=cfg),
    )
    users = SimpleNamespace(upsert_user=AsyncMock())
    # Page 1: return 2 ducks, total of 50 (so 48 more wait on later pages).
    rows = [{"name": "Whiskers", "owner": "alice"},
            {"name": "The Don", "owner": "bob"}]
    duckhunt = SimpleNamespace(
        list_named_ducks_global=AsyncMock(return_value=(rows, 50)),
    )
    settings = SimpleNamespace(
        admin_ids=frozenset(),
        duckhunt_action_cooldown_seconds=15,
        duckhunt_duck_lifetime_seconds=86400,
    )
    rt = SimpleNamespace(
        settings=settings, db=SimpleNamespace(), chats=chats, users=users,
        duckhunt=duckhunt, openai=SimpleNamespace(), bot=SimpleNamespace(),
    )
    router = build_router(rt)
    handler = next(
        h.callback for h in router.observers["message"].handlers
        if h.callback.__name__ == "ducknames"
    )
    msg = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="group", title="t"),
        from_user=SimpleNamespace(id=7, is_bot=False, username="u",
                                  first_name="U", last_name=None),
        text="/ducknames", caption=None, message_id=1,
        reply=AsyncMock(),
    )
    await handler(msg)
    body = msg.reply.await_args.args[0]
    assert "Whiskers" in body and "The Don" in body
    assert "50 total" in body
    assert "/ducknames 2" in body
