"""Dale passing a message on to another chat (ipedro/relay.py).

The persona decides whether he does it; these are the parts that can't be
talked around: both chats opted in, one destination found by title, signed
with the asker's name, small, slow, logged. And that nobody but Dale, in a
chat tuned into the ether (or the owner's DM), is ever offered the tool.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from ipedro import bot_messages, introspection, ratelimit, relay, silenced_chats
from ipedro.capabilities import _CANNOT, capability_brief
from ipedro.handlers import chat
from tests.test_addressed import _handler, _mention_rt
from tests.test_captcha_intercept import _msg
from tests.test_identity import _as_hank
from tests.test_tool_loop import _tool_rt

SRC, DEST, OWNER = -100500, -100777, 315660812
ADMIN = 1


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ratelimit.LIMITER.reset()
    silenced_chats._reset_cache_for_tests()
    bot_messages.reset_buffer()
    tracked = []
    monkeypatch.setattr(relay, "track", lambda *a, **k: tracked.append((a, k)))
    yield tracked
    ratelimit.LIMITER.reset()
    silenced_chats._reset_cache_for_tests()


def _rt(dests=None, *, memory=True):
    cfg = SimpleNamespace(memory_enabled=memory)
    return SimpleNamespace(
        db=SimpleNamespace(fetch=AsyncMock(return_value=dests if dests is not None else [
            {"chat_id": DEST, "title": "The Soup Chat"},
        ])),
        bot=SimpleNamespace(send_message=AsyncMock(
            return_value=SimpleNamespace(message_id=555))),
        chats=SimpleNamespace(get_config=AsyncMock(return_value=cfg)),
        memory=SimpleNamespace(record_message=AsyncMock()),
        activity=SimpleNamespace(log=AsyncMock()),
        settings=SimpleNamespace(admin_ids=frozenset({ADMIN})),
    )


def _asker(**over):
    base = dict(chat_id=SRC, chat_type="supergroup", chat_title="Poker Night",
                user_id=7, user_name="Matt")
    base.update(over)
    return relay.Asker(**base)


async def _go(rt, asker=None, chat_="soup", message="the propane is fine"):
    return await relay.pass_it_along(
        rt, asker or _asker(), {"chat": chat_, "message": message},
    )


# ── the tool ─────────────────────────────────────────────────────────────────

def test_the_tool_is_static_and_says_the_default_is_no():
    a, b = json.dumps(relay.tool()), json.dumps(relay.tool())
    assert a == b                                       # cache-stable
    t = relay.tool()
    assert t["name"] == "pass_it_along"
    assert "default answer is no" in t["description"]
    assert set(t["input_schema"]["properties"]) == {"chat", "message"}
    assert t["input_schema"]["required"] == ["chat", "message"]


def test_nothing_changes_for_a_chat_that_is_not_offered_it():
    """The tool list a chat had before this existed is the list it has now:
    byte for byte, so no group's prompt cache is rebuilt by it."""
    for owner_dm in (False, True):
        assert introspection.tools_for(owner_dm=owner_dm) == \
            introspection.tools_for(owner_dm=owner_dm, relay_on=False)
        assert "pass_it_along" not in introspection.tool_names(owner_dm=owner_dm)
    on = introspection.tools_for(owner_dm=False, relay_on=True)
    assert [t["name"] for t in on] == ["check_my_records", "pass_it_along"]


def test_the_brief_only_mentions_it_when_it_is_offered():
    plain = capability_brief()
    assert "pass_it_along" not in plain and _CANNOT in plain
    assert plain == capability_brief(can_relay=False)
    offered = capability_brief(can_relay=True)
    assert "pass_it_along" in offered and "default answer is no" in offered
    assert "contact people who aren't here" not in offered    # no contradiction
    assert "leave the chat" in offered


@pytest.mark.asyncio
async def test_a_tool_that_was_not_offered_is_refused_without_a_send():
    rt = _rt()
    text, is_error = await introspection.run_tool(
        rt, chat_id=SRC, owner_dm=False, name="pass_it_along",
        args={"chat": "soup", "message": "hi"}, asker=None,
    )
    assert is_error is True
    rt.db.fetch.assert_not_awaited()
    rt.bot.send_message.assert_not_awaited()


# ── where it can go ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_it_goes_to_the_one_tuned_in_group_that_matches():
    rt = _rt()
    text, is_error = await _go(rt)
    assert (text, is_error) == ("Sent to The Soup Chat.", False)
    sent = rt.bot.send_message.await_args
    assert sent.args[0] == DEST and sent.kwargs["disable_notification"] is True


@pytest.mark.asyncio
async def test_the_lookup_only_considers_opted_in_groups_other_than_this_one():
    rt = _rt()
    await _go(rt)
    sql, *params = rt.db.fetch.await_args.args
    for needle in ("ether_enabled = TRUE", "type IN ('group', 'supergroup')",
                   "response_policy <> 'commands'", "c.chat_id <> $1"):
        assert needle in sql, needle
    assert params[0] == SRC


@pytest.mark.asyncio
async def test_like_wildcards_in_a_name_are_literal():
    rt = _rt()
    await _go(rt, chat_="100%_real")
    assert rt.db.fetch.await_args.args[2] == "%100!%!_real%"


@pytest.mark.asyncio
async def test_no_match_sends_nothing_and_names_no_chat():
    rt = _rt(dests=[])
    text, is_error = await _go(rt)
    assert is_error is True and "Nothing was sent" in text
    rt.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_matches_is_not_a_guess():
    rt = _rt(dests=[{"chat_id": DEST, "title": "Soup A"},
                    {"chat_id": DEST - 1, "title": "Soup B"}])
    text, is_error = await _go(rt)
    assert is_error is True
    assert "Soup" not in text                        # no listing of chats
    rt.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_silenced_chat_is_not_a_destination():
    silenced_chats._cache.add(DEST)
    rt = _rt()
    text, is_error = await _go(rt)
    assert is_error is True
    rt.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"chat": "", "message": "hi"},
    {"chat": "soup", "message": ""},
    {"chat": "soup", "message": "   "},
    {"message": "hi"},
])
async def test_it_needs_a_chat_and_something_to_say(args):
    rt = _rt()
    _, is_error = await relay.pass_it_along(rt, _asker(), args)
    assert is_error is True
    rt.bot.send_message.assert_not_awaited()


# ── what goes out ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_it_is_signed_with_the_asker_and_the_chat_it_came_from():
    rt = _rt()
    await _go(rt, message="the propane is fine")
    assert rt.bot.send_message.await_args.args[1] == (
        "Matt, over in Poker Night, had me pass this along:\n\n"
        "the propane is fine"
    )


@pytest.mark.asyncio
async def test_from_a_private_chat_it_names_no_chat():
    rt = _rt()
    await _go(rt, _asker(chat_type="private", chat_title=None, chat_id=OWNER))
    assert rt.bot.send_message.await_args.args[1].startswith(
        "Matt had me pass this along:")


@pytest.mark.asyncio
async def test_the_signature_cannot_be_forged_or_used_to_ping_people():
    rt = _rt()
    await _go(rt, _asker(user_name="@everyone‮ evil", chat_title="@a\x00b"),
              message="ping @someone and @else")
    out = rt.bot.send_message.await_args.args[1]
    assert "@" not in out and "‮" not in out and "\x00" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "look at https://example.com", "http://x.y", "go to www.example.com",
    "t.me/somechannel", "see telegram.me/x", "tg://resolve?domain=x",
])
async def test_links_do_not_go_through(message):
    rt = _rt()
    text, is_error = await _go(rt, message=message)
    assert is_error is True and "Links" in text
    rt.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_long_message_is_cut_to_the_limit():
    rt = _rt()
    await _go(rt, message="x" * 5000)
    body = rt.bot.send_message.await_args.args[1].split("\n\n", 1)[1]
    assert len(body) == relay.MAX_CHARS


# ── slow ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_person_gets_two_an_hour():
    rt = _rt()
    for _ in range(2):
        assert (await _go(rt, _asker(user_id=7, chat_id=SRC)))[1] is False
    ratelimit.LIMITER._hits.pop(("relay", "dest", DEST), None)   # isolate the person limit
    text, is_error = await _go(rt, _asker(user_id=7, chat_id=SRC))
    assert is_error is True and "Nothing was sent" in text
    assert rt.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_a_chat_gets_three_an_hour_however_many_people_ask():
    rt = _rt()
    outcomes = []
    for uid in (11, 12, 13, 14):
        ratelimit.LIMITER._hits.pop(("relay", "dest", DEST), None)
        outcomes.append((await _go(rt, _asker(user_id=uid)))[1])
    assert outcomes == [False, False, False, True]


@pytest.mark.asyncio
async def test_a_destination_only_takes_two_an_hour_from_anyone():
    rt = _rt()
    outcomes = [
        (await _go(rt, _asker(user_id=uid, chat_id=src)))[1]
        for uid, src in ((11, SRC), (12, SRC - 1), (13, SRC - 2))
    ]
    assert outcomes == [False, False, True]


@pytest.mark.asyncio
async def test_bot_admins_are_not_held_to_the_personal_limit():
    rt = _rt()
    outcomes = []
    for i in range(5):
        ratelimit.LIMITER._hits.pop(("relay", "dest", DEST), None)
        outcomes.append((await _go(rt, _asker(user_id=ADMIN, chat_id=SRC - i)))[1])
    assert outcomes == [False] * 5


@pytest.mark.asyncio
async def test_a_refused_attempt_spends_nothing():
    rt = _rt(dests=[])
    for _ in range(10):
        await _go(rt)
    assert ratelimit.LIMITER._hits == {}


# ── after it's sent ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_destination_remembers_it_and_both_chats_log_it(_clean):
    rt = _rt()
    await _go(rt)
    (args, _), = _clean
    assert args[:2] == (DEST, 555)                         # tracked as his send
    rec = rt.memory.record_message.await_args.kwargs
    assert rec["chat_id"] == DEST and rec["role"] == "assistant"
    assert rec["message_id"] == 555 and "pass this along" in rec["content"]
    events = [c.args[:2] for c in rt.activity.log.await_args_list]
    assert events == [(SRC, "relay"), (DEST, "relay_in")]


@pytest.mark.asyncio
async def test_a_chat_with_memory_off_keeps_nothing():
    rt = _rt(memory=False)
    await _go(rt)
    rt.memory.record_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_bookkeeping_that_fails_never_unsends_or_hides_the_send():
    rt = _rt()
    rt.memory.record_message.side_effect = RuntimeError("db down")
    rt.activity.log.side_effect = RuntimeError("db down")
    text, is_error = await _go(rt)
    assert (text, is_error) == ("Sent to The Soup Chat.", False)


@pytest.mark.asyncio
async def test_a_chat_that_refuses_us_comes_off_the_ether(monkeypatch):
    dropped = AsyncMock()
    monkeypatch.setattr(relay, "_drop_dead_destination", dropped)
    rt = _rt()
    rt.bot.send_message.side_effect = TelegramForbiddenError(
        method=None, message="bot was kicked")
    text, is_error = await _go(rt)
    assert is_error is True and "reachable" in text
    dropped.assert_awaited_once_with(rt.db, DEST)
    rt.activity.log.assert_not_awaited()
    rt.memory.record_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_one_off_failure_is_not_taken_for_a_kick(monkeypatch):
    dropped = AsyncMock()
    monkeypatch.setattr(relay, "_drop_dead_destination", dropped)
    rt = _rt()
    rt.bot.send_message.side_effect = RuntimeError("timeout")
    text, is_error = await _go(rt)
    assert is_error is True and "didn't get through" in text
    dropped.assert_not_awaited()


# ── who is offered it, in the real reply path ────────────────────────────────

def _group(rt, *, ether):
    rt.chats.get_config.return_value.ether_enabled = ether
    return rt


@pytest.mark.asyncio
async def test_dale_in_a_tuned_in_group_is_offered_it(monkeypatch):
    rt, captured = _tool_rt(monkeypatch)
    _group(rt, ether=True)
    msg = _msg(text="dale please", chat_id=SRC)
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    kw = rt.openai.chat_with_tools.await_args.kwargs
    assert [t["name"] for t in kw["tools"]] == ["check_my_records", "pass_it_along"]
    assert "pass_it_along" in captured["capabilities"]


@pytest.mark.asyncio
async def test_a_group_not_tuned_in_is_not(monkeypatch):
    rt, captured = _tool_rt(monkeypatch)
    _group(rt, ether=False)
    msg = _msg(text="dale please", chat_id=SRC)
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    kw = rt.openai.chat_with_tools.await_args.kwargs
    assert [t["name"] for t in kw["tools"]] == ["check_my_records"]
    assert "pass_it_along" not in captured["capabilities"]


@pytest.mark.asyncio
async def test_the_owner_dm_is_offered_it_whatever_the_ether_says(monkeypatch):
    rt, _ = _tool_rt(monkeypatch)
    _group(rt, ether=False)
    msg = _msg(text="dale tell soup", chat_id=OWNER, user_id=OWNER)
    msg.chat.type = "private"
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    names = [t["name"] for t in rt.openai.chat_with_tools.await_args.kwargs["tools"]]
    assert names[-1] == "pass_it_along"


@pytest.mark.asyncio
async def test_a_private_chat_with_anyone_else_is_not(monkeypatch):
    rt, _ = _tool_rt(monkeypatch)
    _group(rt, ether=True)
    msg = _msg(text="dale tell soup", chat_id=999, user_id=999)
    msg.chat.type = "private"
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    names = [t["name"] for t in rt.openai.chat_with_tools.await_args.kwargs["tools"]]
    assert "pass_it_along" not in names


@pytest.mark.asyncio
async def test_no_other_bot_is_ever_offered_it(monkeypatch):
    rt, captured = _tool_rt(monkeypatch)
    _as_hank(rt)
    _group(rt, ether=True)
    msg = _msg(text="hank please", chat_id=SRC)
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    names = [t["name"] for t in rt.openai.chat_with_tools.await_args.kwargs["tools"]]
    assert "pass_it_along" not in names
    assert "pass_it_along" not in captured["capabilities"]


@pytest.mark.asyncio
async def test_the_runner_signs_with_whoever_is_speaking_in_this_chat(monkeypatch):
    rt, _ = _tool_rt(monkeypatch)
    _group(rt, ether=True)
    seen = {}

    async def fake(rt_, asker, args):
        seen["asker"], seen["args"] = asker, args
        return "Sent to X.", False

    monkeypatch.setattr(relay, "pass_it_along", fake)
    msg = _msg(text="dale please", chat_id=SRC, user_id=42)
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    run_tool = rt.openai.chat_with_tools.await_args.kwargs["run_tool"]
    out = await run_tool("pass_it_along", {"chat": "soup", "message": "hi"})
    assert out == ("Sent to X.", False)
    assert seen["asker"].chat_id == SRC and seen["asker"].user_id == 42
    assert seen["asker"].chat_type == "group"
