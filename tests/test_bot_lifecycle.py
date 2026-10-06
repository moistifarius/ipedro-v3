"""Starting and stopping the process.

aiogram's start_polling installs its own SIGINT/SIGTERM handlers unless told
not to, which replaced ours: on every `docker stop` polling ended behind
bot.py's back, the unconditional stop_polling() then raised "Polling is not
started", and a polling task that died for a real reason (a revoked token)
was reported as that instead. Handlers still running when the stop arrived
were also abandoned under a closed session.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import bot as bot_mod


class _Dispatcher:
    def __init__(self, polling):
        self._polling = polling
        self.start_kwargs = None
        self.stop_polling = AsyncMock()
        self._handle_update_tasks: set[asyncio.Task] = set()

    def resolve_used_update_types(self):
        return ["message"]

    async def start_polling(self, bot, **kwargs):
        self.start_kwargs = kwargs
        await self._polling()


@pytest.fixture
def harness(monkeypatch):
    async def idle(*a, **k):                  # every background loop
        return None

    for name in ("run_spawner", "run_share_photo_loop", "run_reminders_loop",
                 "run_celebrations_loop", "run_comic_loop", "run_ambient_loops",
                 "run_monthly_recap_loop"):
        monkeypatch.setattr(bot_mod, name, idle)
    monkeypatch.setattr(bot_mod.hub, "run", idle)
    monkeypatch.setattr(bot_mod, "get_settings",
                        lambda: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(bot_mod, "configure_logging", lambda level: None)
    session = SimpleNamespace(close=AsyncMock())
    rt = SimpleNamespace(bot=SimpleNamespace(session=session),
                         db=SimpleNamespace(close=AsyncMock()),
                         openai=SimpleNamespace())

    async def build_runtime(settings):
        return rt

    monkeypatch.setattr(bot_mod, "build_runtime", build_runtime)
    state = SimpleNamespace(rt=rt, dp=None)

    def use(polling):
        state.dp = _Dispatcher(polling)
        monkeypatch.setattr(bot_mod, "build_dispatcher", lambda rt_: state.dp)

    state.use = use
    return state


@pytest.mark.asyncio
async def test_our_signal_handlers_are_not_replaced_by_aiogrammers(harness):
    async def returns_at_once():
        return None

    harness.use(returns_at_once)
    await bot_mod.run()
    assert harness.dp.start_kwargs["handle_signals"] is False


@pytest.mark.asyncio
async def test_polling_that_already_ended_is_not_stopped_a_second_time(harness):
    """stop_polling() on a dispatcher that isn't polling raises
    RuntimeError, which turned every clean shutdown into a traceback."""
    async def returns_at_once():
        return None

    harness.use(returns_at_once)
    await bot_mod.run()                       # must not raise
    harness.dp.stop_polling.assert_not_awaited()
    harness.rt.db.close.assert_awaited_once()          # cleanup still happened
    harness.rt.bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_polling_failure_is_reported_as_itself(harness, caplog):
    async def dies():
        raise PermissionError("Unauthorized: token revoked")

    harness.use(dies)
    with caplog.at_level(logging.ERROR, logger="ipedro.bot"):
        await bot_mod.run()
    assert any("token revoked" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_stop_signal_stops_polling_that_is_still_running(harness):
    import signal

    registered = {}
    loop = asyncio.get_running_loop()
    loop.add_signal_handler = lambda sig, cb, *a: registered.setdefault(sig, cb)

    async def runs_forever():
        await asyncio.sleep(60)

    harness.use(runs_forever)
    task = asyncio.create_task(bot_mod.run())
    for _ in range(20):
        await asyncio.sleep(0)
    assert signal.SIGTERM in registered          # bot.py's handler, not aiogram's
    registered[signal.SIGTERM]()                 # `docker stop`
    await asyncio.wait_for(task, 5)
    harness.dp.stop_polling.assert_awaited_once()


# ── in-flight handlers get to finish ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_drain_waits_for_a_handler_that_is_still_replying():
    dp = _Dispatcher(None)
    finished = []

    async def handler():
        await asyncio.sleep(0.05)
        finished.append(True)

    dp._handle_update_tasks.add(asyncio.create_task(handler()))
    await bot_mod._drain_handlers(dp, timeout=2)
    assert finished == [True]


@pytest.mark.asyncio
async def test_drain_gives_up_on_a_stuck_handler():
    dp = _Dispatcher(None)
    stuck = asyncio.create_task(asyncio.sleep(60))
    dp._handle_update_tasks.add(stuck)
    await bot_mod._drain_handlers(dp, timeout=0.05)
    await asyncio.sleep(0)
    assert stuck.cancelled()


@pytest.mark.asyncio
async def test_drain_with_nothing_running_is_instant():
    await bot_mod._drain_handlers(_Dispatcher(None))
