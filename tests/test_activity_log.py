"""The durable 'why did/didn't the bot reply' activity log.

Every _log_activity() call site in on_message is wired to a real mock
here — not relying on the try/except in _log_activity to silently swallow
a missing rt.activity (which is what every OTHER test file's bare
SimpleNamespace rt would otherwise do, masking a real wiring break).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers import chat
from tests.test_addressed import _mention_rt
from tests.test_captcha_intercept import _msg, _rt_with


def _handler(rt):
    router = chat.build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == "on_message")


def _rt(*, policy="mention", automod=True, ambient_probability=0.0):
    rt = _rt_with()
    cfg = rt.chats.get_config.return_value
    cfg.response_policy = policy
    cfg.automod_enabled = automod
    cfg.ambient_probability = ambient_probability
    # capability_brief() walks these to describe what's scheduled; _rt_with
    # was built for the (unrelated) captcha intercept and doesn't set them.
    for field in (
        "monthly_recap_enabled", "share_photo_enabled", "comic_enabled",
        "fortune_enabled", "ether_enabled",
    ):
        setattr(cfg, field, False)
    return rt


@pytest.mark.asyncio
async def test_thanks_pedro_logs_itself():
    rt = _rt()
    await _handler(rt)(_msg(text="thanks dale appreciate it"))
    rt.activity.log.assert_awaited_once()
    args = rt.activity.log.await_args.args
    assert args[1] == "thanks_pedro"


@pytest.mark.asyncio
async def test_automod_text_trigger_logs_itself():
    rt = _rt()
    await _handler(rt)(_msg(text="thats gay"))
    rt.activity.log.assert_awaited_once()
    args = rt.activity.log.await_args.args
    assert args[1] == "automod"
    assert "text trigger" in args[2]


@pytest.mark.asyncio
async def test_ambient_gif_logs_itself(monkeypatch):
    monkeypatch.setattr(chat.dale, "send_random", AsyncMock(return_value=True))
    monkeypatch.setattr(chat, "_DALE_GIF_PROBABILITY", 1.0)
    monkeypatch.setattr(chat, "_REACT_PROBABILITY", 0.0)
    rt = _rt()
    await _handler(rt)(_msg(text="anyway the kitchen tap is dripping"))
    rt.activity.log.assert_awaited_once()
    args = rt.activity.log.await_args.args
    assert args[1] == "ambient_gif"


@pytest.mark.asyncio
async def test_unaddressed_plain_chatter_logs_no_reply():
    rt = _rt()
    await _handler(rt)(_msg(text="anyway the kitchen tap is dripping"))
    rt.activity.log.assert_awaited_once()
    args = rt.activity.log.await_args.args
    assert args[1] == "no_reply"
    assert "policy=mention" in args[2]


@pytest.mark.asyncio
async def test_credit_line_logs_itself_distinctly_from_no_reply(monkeypatch):
    monkeypatch.setattr(chat, "_CREDIT_PROBABILITY", 1.0)
    rt = _mention_rt(monkeypatch)
    await _handler(rt)(_msg(text="that plan is great honestly"))
    rt.activity.log.assert_awaited_once()
    args = rt.activity.log.await_args.args
    assert args[1] == "credit_line"


@pytest.mark.asyncio
async def test_ai_reply_logs_addressed_reason(monkeypatch):
    rt = _mention_rt(monkeypatch)
    await _handler(rt)(_msg(text="dale what do you think"))
    rt.activity.log.assert_awaited_once()
    args = rt.activity.log.await_args.args
    assert args[1] == "ai_reply"
    assert args[2].startswith("addressed:")


@pytest.mark.asyncio
async def test_ai_reply_logs_reply_to_reason(monkeypatch):
    rt = _mention_rt(monkeypatch)
    reply_to = SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=True),
        message_id=55,
    )
    await _handler(rt)(_msg(text="what about this", reply_to=reply_to))
    rt.activity.log.assert_awaited_once()
    args = rt.activity.log.await_args.args
    assert args[1] == "ai_reply"
    assert args[2].startswith("reply-to:")


@pytest.mark.asyncio
async def test_ai_reply_logs_ambient_roll_reason(monkeypatch):
    """response_policy=ambient, no mention, no reply-to — should_respond's
    own probability roll is what let this one through."""
    rt = _mention_rt(monkeypatch)
    cfg = rt.chats.get_config.return_value
    cfg.response_policy = "ambient"
    cfg.ambient_probability = 1.0
    await _handler(rt)(_msg(text="anyway the kitchen tap is dripping"))
    rt.activity.log.assert_awaited_once()
    args = rt.activity.log.await_args.args
    assert args[1] == "ai_reply"
    assert args[2].startswith("ambient-roll:")


@pytest.mark.asyncio
async def test_activity_log_failure_never_breaks_the_handler(monkeypatch):
    """Same bookkeeping rule as everywhere else in this file: a DB hiccup
    here must not stop the user from getting their reply."""
    rt = _rt()
    rt.activity.log = AsyncMock(side_effect=RuntimeError("db hiccup"))
    msg = _msg(text="thanks dale appreciate it")
    await _handler(rt)(msg)  # must not raise
    msg.reply.assert_awaited_once()
