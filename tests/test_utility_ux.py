"""Utility-command UX guards: memory-off messaging, date-saving honesty.

Regressions covered:
  - history commands used to answer "no data" in memory-off chats, which
    reads as broken rather than disabled;
  - /anniversary silently overwrote the previous anniversary (uniqueness
    key ignored the name);
  - /birthday @unknown-user saved an ownerless date and claimed success.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers.utility import build_router


def _handler(rt, name):
    router = build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == name)


def _rt(*, memory_enabled=True, user_lookup=None):
    cfg = SimpleNamespace(
        memory_enabled=memory_enabled, duckhunt_enabled=True,
        response_policy="always", automod_enabled=True,
        vision_enabled=False,
    )
    executed: list[tuple] = []

    async def execute(query, *args):
        executed.append((query, args))
        return "OK"

    return SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset()),
        chats=SimpleNamespace(
            upsert_chat=AsyncMock(), get_config=AsyncMock(return_value=cfg),
            upsert_default_config=AsyncMock(return_value=cfg),
        ),
        users=SimpleNamespace(upsert_user=AsyncMock()),
        db=SimpleNamespace(
            fetchrow=AsyncMock(return_value=user_lookup),
            fetch=AsyncMock(return_value=[]),
            fetchval=AsyncMock(return_value=0),
            execute=AsyncMock(side_effect=execute),
        ),
        openai=SimpleNamespace(cheap_completion=AsyncMock(return_value="x")),
        bot=SimpleNamespace(send_chat_action=AsyncMock()),
    ), executed


def _msg(text):
    return SimpleNamespace(
        chat=SimpleNamespace(id=42, type="group", title="t"),
        from_user=SimpleNamespace(id=7, is_bot=False, username="u",
                                  first_name="U", last_name=None),
        text=text, message_id=100, reply_to_message=None,
        reply=AsyncMock(return_value=SimpleNamespace(message_id=101)),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=102)),
    )


@pytest.mark.asyncio
async def test_memory_off_history_command_says_so():
    rt, _ = _rt(memory_enabled=False)
    handler = _handler(rt, "tldr")
    msg = _msg("/tldr")
    await handler(msg)
    text = msg.reply.await_args.args[0].lower()
    assert "memory is off" in text
    rt.openai.cheap_completion.assert_not_awaited()   # stopped at the guard


@pytest.mark.asyncio
async def test_memory_on_does_not_trip_the_guard():
    rt, _ = _rt(memory_enabled=True)
    handler = _handler(rt, "tldr")
    msg = _msg("/tldr")
    await handler(msg)
    text = (msg.reply.await_args.args[0] or "").lower()
    assert "memory is off" not in text


@pytest.mark.asyncio
async def test_named_anniversaries_do_not_collide():
    rt, executed = _rt()
    handler = _handler(rt, "anniversary")
    await handler(_msg("/anniversary wedding 06-01-2020"))
    await handler(_msg("/anniversary moving-day 09-14-2022"))
    labels = [args[2] for q, args in executed if "chat_dates" in q]
    assert labels == ["anniversary:wedding", "anniversary:moving-day"]


@pytest.mark.asyncio
async def test_plain_birthday_keeps_plain_label():
    rt, executed = _rt()
    handler = _handler(rt, "birthday")
    await handler(_msg("/birthday 06-01"))
    labels = [args[2] for q, args in executed if "chat_dates" in q]
    assert labels == ["birthday"]


@pytest.mark.asyncio
async def test_birthday_unknown_user_errors_instead_of_saving_orphan():
    rt, executed = _rt(user_lookup=None)   # @stranger resolves to nobody
    handler = _handler(rt, "birthday")
    msg = _msg("/birthday @stranger 06-01-1990")
    await handler(msg)
    assert "don't know" in msg.reply.await_args.args[0].lower()
    assert not [q for q, _ in executed if "chat_dates" in q]   # nothing saved
