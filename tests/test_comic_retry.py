"""A comic that can't be posted must not be re-generated every tick.

The pipeline is a model call over the day's chat plus an image generation
(~$0.04), and it ran BEFORE the send. When the send failed (kicked, blocked,
media restricted) nothing was stamped and the chat came due again 10
minutes later: about 144 paid attempts a day, per chat, forever.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from ipedro import comic


def _rows(n=10):
    return [{"role": "user", "content": f"m{n}"} for n in range(n)]


def _world(*, rows=None, scenes="a\nb\nc\nd", image=b"PNG", send=None):
    db = SimpleNamespace(
        fetch=AsyncMock(return_value=_rows() if rows is None else rows),
        execute=AsyncMock(),
    )
    openai = SimpleNamespace(
        cheap_completion=AsyncMock(return_value=scenes),
        generate_image=AsyncMock(return_value=image),
    )
    bot = SimpleNamespace(send_photo=send or AsyncMock(
        return_value=SimpleNamespace(message_id=5)))
    return db, openai, bot


@pytest.mark.asyncio
async def test_a_posted_comic_is_stamped_done():
    db, openai, bot = _world()
    assert await comic._build_and_post(1, bot, db, openai) == comic.POSTED
    await comic._stamp(db, 1, comic.POSTED)
    assert "NOW() WHERE" in db.execute.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    TelegramForbiddenError(method=SimpleNamespace(), message="Forbidden: bot was kicked"),
    TelegramBadRequest(method=SimpleNamespace(), message="Bad Request: not enough rights to send photos"),
])
async def test_a_chat_that_refuses_us_is_done_for_the_day(exc):
    db, openai, bot = _world(send=AsyncMock(side_effect=exc))
    assert await comic._build_and_post(1, bot, db, openai) == comic.UNDELIVERABLE
    await comic._stamp(db, 1, comic.UNDELIVERABLE)
    assert "NOW() WHERE" in db.execute.await_args.args[0]


@pytest.mark.asyncio
async def test_a_failed_attempt_waits_hours_not_ten_minutes():
    db, openai, bot = _world(image=None)               # the image model said no
    assert await comic._build_and_post(1, bot, db, openai) == comic.FAILED
    await comic._stamp(db, 1, comic.FAILED)
    sql, chat_id, back = db.execute.await_args.args
    assert "make_interval" in sql and chat_id == 1
    # stamped far enough back that it is due again in 6h, not 24h and not now
    assert back == int((comic._LOOKBACK - comic._RETRY_AFTER_FAILURE).total_seconds())
    assert comic._RETRY_AFTER_FAILURE.total_seconds() >= 3600


@pytest.mark.asyncio
async def test_a_transient_send_error_also_backs_off():
    db, openai, bot = _world(send=AsyncMock(side_effect=RuntimeError("blip")))
    assert await comic._build_and_post(1, bot, db, openai) == comic.FAILED


@pytest.mark.asyncio
async def test_no_scenes_is_a_failure_that_waits_not_a_free_retry():
    db, openai, bot = _world(scenes=None)
    assert await comic._build_and_post(1, bot, db, openai) == comic.FAILED
    db, openai, bot = _world(scenes="only\ntwo")
    assert await comic._build_and_post(1, bot, db, openai) == comic.FAILED


@pytest.mark.asyncio
async def test_a_quiet_day_spends_nothing_and_stamps_nothing():
    db, openai, bot = _world(rows=_rows(3))
    assert await comic._build_and_post(1, bot, db, openai) == comic.SKIPPED
    openai.cheap_completion.assert_not_awaited()
    openai.generate_image.assert_not_awaited()
    await comic._stamp(db, 1, comic.SKIPPED)
    db.execute.assert_not_awaited()
