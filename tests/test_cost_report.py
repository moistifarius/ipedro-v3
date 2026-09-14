"""/cost and /manage -> Debug -> Cost now share one query + formatter.

Before this, `_mgm_render_cost` was a near-verbatim copy of `cost()`'s
all-chats query and formatting loop, missing the cache-read/write
columns and the cache-hit-rate line the real /cost command shows — the
two admin surfaces had already drifted apart on the same underlying
data. Both now call the same `_cost_report_lines` closure.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers import admin

ADMIN_ID = 7

_ROWS = [
    {"kind": "chat", "calls": 10, "tokens": 5000, "cost": 0.05,
     "prompt": 4000, "cached": 3000, "written": 200},
]


def _rt():
    return SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset({ADMIN_ID})),
        db=SimpleNamespace(fetch=AsyncMock(return_value=_ROWS)),
    )


def _message_handler(rt, name):
    router = admin.build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == name)


def _msg(text="/cost"):
    return SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=ADMIN_ID, is_bot=False, username="a",
                                  first_name="A", last_name=None),
        text=text, message_id=1,
        reply=AsyncMock(return_value=SimpleNamespace(message_id=2)),
    )


@pytest.mark.asyncio
async def test_cost_command_shows_the_cache_hit_rate():
    rt = _rt()
    handler = _message_handler(rt, "cost")
    msg = _msg("/cost")
    await handler(msg)
    out = msg.reply.await_args.args[0]
    assert "cache:" in out and "75%" in out          # 3000 of 4000 read
    assert "TOTAL: $0.0500" in out


@pytest.mark.asyncio
async def test_the_manage_panel_renders_the_same_cache_line():
    """This is the actual regression check: before the dedup, the panel's
    own query never selected the cache columns at all."""
    rt = _rt()
    router = admin.build_router(rt)
    handler = next(
        h.callback for h in router.observers["callback_query"].handlers
        if h.callback.__name__ == "on_mgm"
    )
    cb = SimpleNamespace(
        data="mgm:debug:cost",
        from_user=SimpleNamespace(id=ADMIN_ID),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=42, type="private"),
            edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    await handler(cb)
    body = cb.message.edit_text.await_args.args[0]
    assert "cache:" in body and "75%" in body
