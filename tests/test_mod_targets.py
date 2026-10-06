"""A chat admin may silence members, not the people who run the bot.

/shutup only checked the CALLER's tier. A Telegram chat admin (below a bot
admin) could mute the owner in their chat; commands still worked, so the
owner could undo it, but not until they noticed the bot ignoring them.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers import mod

OWNER, BOT_ADMIN, CHAT_ADMIN, MEMBER = 315660812, 7, 20, 30


@pytest.fixture
def flags(monkeypatch):
    calls = []

    async def set_flag(db, chat_id, user_id, flag, **kw):
        calls.append((chat_id, user_id, flag))

    monkeypatch.setattr(mod, "set_flag", set_flag)
    return calls


def _rt():
    return SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset({OWNER, BOT_ADMIN})),
        bot=SimpleNamespace(get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status="administrator"))),
        db=SimpleNamespace(),
    )


def _handler(rt, name):
    router = mod.build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == name)


def _msg(text, *, caller, target):
    return SimpleNamespace(
        chat=SimpleNamespace(id=-100, type="supergroup"),
        from_user=SimpleNamespace(id=caller, first_name="C", username="c", last_name=None),
        reply_to_message=SimpleNamespace(from_user=SimpleNamespace(
            id=target, first_name="T", username="t", last_name=None, is_bot=False)),
        text=text, reply=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("command,flag", [("shutup", "shutup"), ("snark_at", "snark")])
async def test_a_chat_admin_can_silence_a_member(flags, command, flag):
    rt = _rt()
    await _handler(rt, command)(_msg(f"/{command}", caller=CHAT_ADMIN, target=MEMBER))
    assert flags == [(-100, MEMBER, flag)]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["shutup", "snark_at"])
@pytest.mark.parametrize("target", [OWNER, BOT_ADMIN])
async def test_a_chat_admin_cannot_silence_a_bot_admin(flags, command, target):
    rt = _rt()
    msg = _msg(f"/{command}", caller=CHAT_ADMIN, target=target)
    await _handler(rt, command)(msg)
    assert flags == []
    assert "bot admin" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_a_bot_admin_may_silence_another_bot_admin(flags):
    rt = _rt()
    await _handler(rt, "shutup")(_msg("/shutup", caller=OWNER, target=BOT_ADMIN))
    assert flags == [(-100, BOT_ADMIN, "shutup")]


@pytest.mark.asyncio
@pytest.mark.parametrize("tail,ttl_set", [
    ("", False),                  # no duration: indefinitely, as documented
    (" 30m", True), (" 2h", True), (" 1d", True),
])
async def test_a_duration_that_parses_is_applied(monkeypatch, tail, ttl_set):
    seen = []

    async def set_flag(db, chat_id, user_id, flag, **kw):
        seen.append(kw.get("ttl"))

    monkeypatch.setattr(mod, "set_flag", set_flag)
    msg = _msg(f"/shutup{tail}", caller=CHAT_ADMIN, target=MEMBER)
    await _handler(_rt(), "shutup")(msg)
    assert (seen[0] is not None) is ttl_set


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", ["30mins", "forever", "banana", "2 hours"])
async def test_a_duration_that_does_not_parse_is_refused_not_read_as_forever(flags, junk):
    msg = _msg(f"/shutup {junk}", caller=CHAT_ADMIN, target=MEMBER)
    await _handler(_rt(), "shutup")(msg)
    assert flags == []
    assert "Nothing changed" in msg.reply.await_args.args[0]
