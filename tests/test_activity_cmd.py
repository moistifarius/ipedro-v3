"""Tests for the /activity admin command (ipedro/handlers/admin.py).

/activity surfaces the durable activity_log table — the omniscience
layer — as opposed to /logs (raw program log lines) and /cmdlog (admin
command audit trail).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers.admin import build_router


def _find_handler(router, name: str):
    for h in router.observers["message"].handlers:
        if h.callback.__name__ == name:
            return h.callback
    raise AssertionError(f"handler {name} not registered")


def _msg(*, text: str, user_id: int = 7, chat_type: str = "private") -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=42, type=chat_type, title=None),
        from_user=SimpleNamespace(id=user_id, is_bot=False, username="admin"),
        text=text,
        reply=AsyncMock(),
    )


def _rt(*, admin_ids=frozenset({7}), rows=None) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(admin_ids=admin_ids),
        activity=SimpleNamespace(recent=AsyncMock(return_value=rows or [])),
    )


def _row(**over):
    base = dict(
        chat_id=-100123, message_id=1, event_type="ai_reply",
        detail="addressed: 'hi'", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_activity_refused_for_non_admin():
    rt = _rt(admin_ids=frozenset({999}))
    handler = _find_handler(build_router(rt), "activity_cmd")
    await handler(_msg(text="/activity"))
    rt.activity.recent.assert_not_awaited()


@pytest.mark.asyncio
async def test_activity_with_no_rows_says_so():
    rt = _rt(rows=[])
    handler = _find_handler(build_router(rt), "activity_cmd")
    msg = _msg(text="/activity")
    await handler(msg)
    body = msg.reply.await_args.args[0]
    assert "No activity log entries" in body


@pytest.mark.asyncio
async def test_activity_default_usage_has_no_filters():
    rt = _rt(rows=[_row()])
    handler = _find_handler(build_router(rt), "activity_cmd")
    await handler(_msg(text="/activity"))
    rt.activity.recent.assert_awaited_once_with(chat_id=None, limit=30, event_type=None)


@pytest.mark.asyncio
async def test_activity_parses_limit_then_chat_id_then_type():
    rt = _rt(rows=[_row()])
    handler = _find_handler(build_router(rt), "activity_cmd")
    await handler(_msg(text="/activity 50 -1001234 no_reply"))
    rt.activity.recent.assert_awaited_once_with(
        chat_id=-1001234, limit=50, event_type="no_reply",
    )


@pytest.mark.asyncio
async def test_activity_type_filter_without_a_chat_id():
    rt = _rt(rows=[_row()])
    handler = _find_handler(build_router(rt), "activity_cmd")
    await handler(_msg(text="/activity 50 ai_reply"))
    rt.activity.recent.assert_awaited_once_with(chat_id=None, limit=50, event_type="ai_reply")


@pytest.mark.asyncio
async def test_activity_limit_is_clamped():
    rt = _rt(rows=[_row()])
    handler = _find_handler(build_router(rt), "activity_cmd")
    await handler(_msg(text="/activity 999"))
    rt.activity.recent.assert_awaited_once_with(chat_id=None, limit=200, event_type=None)


@pytest.mark.asyncio
async def test_activity_formats_rows_with_detail():
    rt = _rt(rows=[_row(detail="addressed: 'hi'"), _row(event_type="no_reply", detail=None)])
    handler = _find_handler(build_router(rt), "activity_cmd")
    msg = _msg(text="/activity")
    await handler(msg)
    body = msg.reply.await_args.args[0]
    assert "ai_reply: addressed: 'hi'" in body
    assert "no_reply" in body and "no_reply: " not in body  # no trailing ": " when detail is None
