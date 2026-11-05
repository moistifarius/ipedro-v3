"""/debug_persona explains which persona a chat gets and why."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import personas
from ipedro.handlers.debug import build_router


@pytest.fixture(autouse=True)
def _restore_personas(monkeypatch):
    monkeypatch.setattr(personas, "_default_prompt", personas.DEFAULT_DALE_PROMPT)
    monkeypatch.setattr(personas, "_master_prompt_override", None)


def _handler():
    rt = SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset({7})),
        chats=SimpleNamespace(get_config=AsyncMock(return_value=SimpleNamespace(
            persona="dude", persona_custom=None))),
    )
    router = build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == "debug_persona")


def _msg():
    return SimpleNamespace(
        chat=SimpleNamespace(id=7, type="private"),
        from_user=SimpleNamespace(id=7), reply=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_a_bot_running_its_own_starting_persona_reports_no_override():
    """Every non-Dale bot compared against DALE's prompt and so reported a
    /master_prompt override that nobody had set."""
    personas.set_default_prompt("You are Hank Hill. You sell propane.")
    msg = _msg()
    await _handler()(msg)
    body = msg.reply.await_args.args[0]
    assert "no (using default)" in body
    assert "/master_prompt override" not in body.split("source:")[1].splitlines()[0]
    assert "You are Hank Hill" in body


@pytest.mark.asyncio
async def test_a_real_override_is_still_reported():
    personas.set_default_prompt("You are Hank Hill.")
    personas.set_master_prompt_override("You are Bobby.")
    msg = _msg()
    await _handler()(msg)
    body = msg.reply.await_args.args[0]
    assert "yes (" in body and "Resolved persona source: /master_prompt override" in body
