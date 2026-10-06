"""A duck nobody was told about must not exist.

The duck row is written before the announcement. When the send failed (a
chat that kicked the bot, a Telegram hiccup) the row stayed active: an
invisible duck that blocked every spawn for up to a day.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError

from ipedro.duckhunt import spawner
from ipedro.duckhunt.service import DuckhuntService
from ipedro.handlers import duckhunt as duck_h


def _service():
    return SimpleNamespace(
        active_duck=AsyncMock(return_value=None),
        spawn_duck=AsyncMock(return_value=SimpleNamespace(id=41)),
        retire_unannounced=AsyncMock(),
    )


@pytest.fixture
def always_spawn(monkeypatch):
    monkeypatch.setattr(spawner, "_recent_activity_factor", AsyncMock(return_value=1.0))
    monkeypatch.setattr(spawner, "build_quack_message_for", AsyncMock(return_value="quack"))


@pytest.mark.asyncio
async def test_a_failed_announcement_takes_the_duck_back(always_spawn):
    service = _service()
    bot = SimpleNamespace(send_message=AsyncMock(
        side_effect=TelegramForbiddenError(method=None, message="bot was kicked")))
    await spawner._maybe_spawn(-100, 1.0, bot, service, None,
                               SimpleNamespace(duckhunt_duck_lifetime_seconds=60), None)
    service.retire_unannounced.assert_awaited_once_with(41)


@pytest.mark.asyncio
async def test_a_delivered_announcement_keeps_the_duck(always_spawn):
    service = _service()
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)))
    await spawner._maybe_spawn(-100, 1.0, bot, service, None,
                               SimpleNamespace(duckhunt_duck_lifetime_seconds=60), None)
    service.retire_unannounced.assert_not_awaited()


@pytest.mark.asyncio
async def test_retiring_resolves_only_a_duck_still_unresolved():
    db = SimpleNamespace(execute=AsyncMock())
    await DuckhuntService(db).retire_unannounced(41)
    sql, duck_id = db.execute.await_args.args
    assert duck_id == 41 and "resolved = FALSE" in sql and "'expired'" in sql


@pytest.mark.asyncio
async def test_the_manual_summon_takes_the_duck_back_too(monkeypatch):
    monkeypatch.setattr(duck_h, "get_or_create_chat_config",
                        AsyncMock(return_value=SimpleNamespace(duckhunt_enabled=True)))
    monkeypatch.setattr(duck_h, "build_quack_message_for", AsyncMock(return_value="quack"))
    rt = SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset({7}), duckhunt_duck_lifetime_seconds=60),
        duckhunt=_service(), openai=None,
    )
    router = duck_h.build_router(rt)
    handler = next(h.callback for h in router.observers["message"].handlers
                   if h.callback.__name__ == "duckhunt_cmd")
    msg = SimpleNamespace(
        chat=SimpleNamespace(id=-100, type="supergroup"),
        from_user=SimpleNamespace(id=7), reply=AsyncMock(),
        answer=AsyncMock(side_effect=TelegramForbiddenError(method=None, message="kicked")),
    )
    with pytest.raises(TelegramForbiddenError):
        await handler(msg)
    rt.duckhunt.retire_unannounced.assert_awaited_once_with(41)
