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


# sha256 of json.dumps(tools_for(owner_dm=...)) as the tool definitions were
# before pass_it_along existed (commit 9139d62). A chat that isn't offered the
# relay must be sent exactly these bytes, or its prompt cache is rebuilt.
_PRE_RELAY_TOOLS_SHA = {
    False: "0aafc850750533231659f950993302acbb63e6b51089f3928e8f4b22d13e56e1",
    True: "f7804204dac1771438f3847c0aa191a6fa2fc0e1a6a4534ed4680a7cc51750ff",
}


def test_a_chat_that_is_not_offered_it_gets_the_tool_bytes_it_always_had():
    import hashlib
    for owner_dm, expected in _PRE_RELAY_TOOLS_SHA.items():
        for kwargs in ({}, {"relay_on": False}):
            got = hashlib.sha256(json.dumps(
                introspection.tools_for(owner_dm=owner_dm, **kwargs)
            ).encode()).hexdigest()
            assert got == expected, (owner_dm, kwargs)
        assert "pass_it_along" not in introspection.tool_names(owner_dm=owner_dm)
    on = introspection.tools_for(owner_dm=False, relay_on=True)
    assert [t["name"] for t in on] == ["check_my_records", "pass_it_along"]


def test_the_relay_rows_are_askable_only_where_the_relay_is_offered():
    def kinds(tools):
        return tools[0]["input_schema"]["properties"]["event_type"]["enum"]

    off = introspection.tools_for(owner_dm=False)
    on = introspection.tools_for(owner_dm=False, relay_on=True)
    assert kinds(off) == list(introspection.ACTIVITY_EVENT_TYPES)
    assert kinds(on) == list(introspection.ACTIVITY_EVENT_TYPES) + [
        relay.EVENT_SENT, relay.EVENT_RECEIVED]
    desc = on[0]["input_schema"]["properties"]["event_type"]["description"]
    assert relay.EVENT_SENT in desc and relay.EVENT_RECEIVED in desc
    assert "relay" not in off[0]["input_schema"]["properties"]["event_type"]["description"]


@pytest.mark.asyncio
@pytest.mark.parametrize("offered, expected", [(True, "relay"), (False, None)])
async def test_a_relay_row_can_be_asked_for_only_when_offered(offered, expected):
    rt = SimpleNamespace(
        activity=SimpleNamespace(recent=AsyncMock(return_value=[])),
        settings=SimpleNamespace(tzinfo=__import__("datetime").timezone.utc),
        db=SimpleNamespace(fetch=AsyncMock(return_value=[])),
    )
    await introspection.run_tool(
        rt, chat_id=SRC, owner_dm=False, name="check_my_records",
        args={"event_type": "relay"}, asker=_asker() if offered else None,
    )
    assert rt.activity.recent.await_args.kwargs["event_type"] == expected


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
    sql = " ".join(sql.split())
    for needle in (
        "WHERE c.type IN ('group', 'supergroup')",
        "AND cc.ether_enabled = TRUE",
        "AND cc.response_policy <> 'commands'",
        "AND c.chat_id <> $1",
        "AND c.chat_id <> ALL($3::bigint[])",
        "AND c.title ILIKE $2 ESCAPE '!'",
        "JOIN chat_config cc ON cc.chat_id = c.chat_id",
        "LIMIT 5",
    ):
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
@pytest.mark.parametrize("name, title", [
    ("@everyone\u202e evil", "@a\x00b"),
    ("@@victim", "@@@victim"),                      # one pass used to leave one @
    ("/start", "/ether"),                            # a command at the very start
    ("evil.com", "bit.ly/x"),                        # a link in a name or a title
    ("see https://x.co", "t.me/joinchat"),
])
async def test_the_signature_cannot_ping_link_or_command(name, title):
    rt = _rt()
    await _go(rt, _asker(user_name=name, chat_title=title))
    out = rt.bot.send_message.await_args.args[1]
    head = out.split("\n\n", 1)[0]
    assert "@" not in head and "\u202e" not in head and "\x00" not in head
    assert not relay._LINK_RE.search(head) and not relay._COMMAND_RE.search(head)
    assert not head.startswith("/")
    assert head.endswith("had me pass this along:")


@pytest.mark.asyncio
async def test_a_name_that_is_all_link_falls_back_to_someone():
    rt = _rt()
    await _go(rt, _asker(user_name="evil.com", chat_title="bit.ly/x"))
    assert rt.bot.send_message.await_args.args[1].startswith(
        "Someone had me pass this along:")


@pytest.mark.asyncio
@pytest.mark.parametrize("message, why", [
    ("ping @someone", "@-mentions"), ("ping @@bob", "@-mentions"),
    ("try /ether now", "/commands"), ("/start", "/commands"),
])
async def test_mentions_and_commands_are_refused_not_silently_rewritten(message, why):
    rt = _rt()
    text, is_error = await _go(rt, message=message)
    assert is_error is True and why in text and "Nothing was sent" in text
    rt.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["and/or whatever", "1/2 of it", "N/A", "that was /s"])
async def test_ordinary_slashes_are_fine(message):
    rt = _rt()
    assert (await _go(rt, message=message))[1] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("ch", [
    "\x01", "\x08", "\x0b", "\x1f", "\x7f",
    "\u202a", "\u202e", "\u2066", "\u2069",
])
async def test_control_and_direction_characters_never_go_out(ch):
    rt = _rt()
    await _go(rt, _asker(user_name=f"Ma{ch}tt"), message=f"pro{ch}pane")
    out = rt.bot.send_message.await_args.args[1]
    assert ch not in out
    assert out.endswith("propane") and out.startswith("Matt, over in")


@pytest.mark.asyncio
async def test_newlines_survive_in_the_body():
    rt = _rt()
    await _go(rt, message="one\ntwo")
    assert rt.bot.send_message.await_args.args[1].endswith("one\ntwo")


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "look at https://example.com", "http://x.y", "go to www.example.com",
    "t.me/somechannel", "see telegram.me/x", "tg://resolve?domain=x",
    "HTTPS://EXAMPLE.COM", "evil.com", "go to evil.com now", "bit.ly/x",
    "youtu.be/abc", "amzn.to/x", "goo.gl", "ftp://x", "ton://transfer",
    "mailto:a", "sketchy.xyz", "pay at example.io/x",
])
async def test_links_do_not_go_through(message):
    rt = _rt()
    text, is_error = await _go(rt, message=message)
    assert is_error is True and "Links" in text
    rt.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_limit_is_five_hundred_characters_and_over_is_refused_not_cut():
    assert relay.MAX_CHARS == 500
    rt = _rt()
    assert (await _go(rt, message="x" * 500))[1] is False
    body = rt.bot.send_message.await_args.args[1].split("\n\n", 1)[1]
    assert body == "x" * 500
    text, is_error = await _go(rt, message="x" * 501)
    assert is_error is True and "too long" in text and "Nothing was sent" in text
    assert rt.bot.send_message.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "St.Louis is nice", "see you at 5.30 ok", "e.g. this", "Mr.Gribble",
    "node.js is fine", "it said ok.fine",
])
async def test_ordinary_dots_are_not_a_link(message):
    rt = _rt()
    assert (await _go(rt, message=message))[1] is False


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
    assert is_error is True and "may not have gone through" in text
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


# ── the hourly limits, pinned (windows, buckets, refunds) ────────────────────

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(ratelimit.LIMITER, "_clock", c)
    return c


@pytest.mark.asyncio
async def test_a_slot_comes_back_after_exactly_an_hour(clock):
    rt = _rt()
    assert (await _go(rt, _asker(user_id=7)))[1] is False
    assert (await _go(rt, _asker(user_id=7)))[1] is False
    clock.now += 3599
    assert (await _go(rt, _asker(user_id=7)))[1] is True          # still held
    clock.now += 2
    assert (await _go(rt, _asker(user_id=7)))[1] is False         # an hour on


@pytest.mark.asyncio
async def test_the_limits_are_per_person_per_chat_and_per_destination(clock):
    rt = _rt(dests=[{"chat_id": DEST, "title": "Soup"},])
    # person 7 uses both of theirs; person 8, same chat, still has theirs
    for _ in range(2):
        await _go(rt, _asker(user_id=7, chat_id=SRC))
    assert (await _go(rt, _asker(user_id=7, chat_id=SRC - 1)))[1] is True    # the person
    # the destination has had two now, from anyone
    assert (await _go(rt, _asker(user_id=8, chat_id=SRC - 2)))[1] is True
    # a different destination is a different bucket
    rt2 = _rt(dests=[{"chat_id": DEST - 5, "title": "Soup"}])
    assert (await _go(rt2, _asker(user_id=8, chat_id=SRC - 2)))[1] is False


@pytest.mark.asyncio
async def test_bot_admins_skip_only_the_personal_limit(clock):
    rt = _rt()
    # 3 an hour from one chat, admin or not; each to a fresh destination
    outcomes = []
    for i in range(4):
        rt.db.fetch.return_value = [{"chat_id": DEST - i, "title": "Soup"}]
        outcomes.append((await _go(rt, _asker(user_id=ADMIN, chat_id=SRC)))[1])
    assert outcomes == [False, False, False, True]
    # and the destination cap holds for admins too
    rt.db.fetch.return_value = [{"chat_id": DEST - 10, "title": "Soup"}]
    outcomes = [(await _go(rt, _asker(user_id=ADMIN, chat_id=SRC - 20 - i)))[1]
                for i in range(3)]
    assert outcomes == [False, False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [0, None, 1087968824, 136817688])
async def test_no_one_to_sign_for_means_nothing_is_sent(user_id):
    rt = _rt()
    text, is_error = await _go(rt, _asker(user_id=user_id))
    assert is_error is True and "Nothing was sent" in text
    rt.bot.send_message.assert_not_awaited()
    assert ratelimit.LIMITER._hits == {}


@pytest.mark.asyncio
async def test_a_refusal_by_the_destination_gives_the_asker_their_slots_back(clock):
    rt = _rt()
    for uid, src in ((11, SRC), (12, SRC - 1)):
        await _go(rt, _asker(user_id=uid, chat_id=src))     # fills the destination
    text, is_error = await _go(rt, _asker(user_id=13, chat_id=SRC - 2))
    assert is_error is True
    assert not ratelimit.LIMITER._hits.get(("relay", "user", 13))
    assert not ratelimit.LIMITER._hits.get(("relay", "chat", SRC - 2))
    assert len(ratelimit.LIMITER._hits[("relay", "dest", DEST)]) == 2


@pytest.mark.asyncio
async def test_a_send_telegram_refused_gives_every_slot_back(monkeypatch):
    monkeypatch.setattr(relay, "_drop_dead_destination", AsyncMock())
    rt = _rt()
    rt.bot.send_message.side_effect = TelegramBadRequest(
        method=None, message="Bad Request: TOPIC_CLOSED")
    _, is_error = await _go(rt)
    assert is_error is True
    assert not any(ratelimit.LIMITER._hits.values())


@pytest.mark.asyncio
async def test_a_send_that_may_have_gone_out_keeps_its_slots(monkeypatch):
    rt = _rt()
    rt.bot.send_message.side_effect = RuntimeError("timed out")
    await _go(rt)
    assert len(ratelimit.LIMITER._hits[("relay", "dest", DEST)]) == 1
    assert len(ratelimit.LIMITER._hits[("relay", "user", 7)]) == 1


def test_the_limiter_counts_every_use_including_the_five_hundredth():
    """The idle-key sweep used to run before the use was recorded and delete
    the very key being checked, so every 500th use of an idle key vanished."""
    lim = ratelimit.Limiter(clock=lambda: 1000.0)
    for i in range(499):
        lim.check(("other", i), limit=5, window=3600.0)
    assert lim.check(("k",), limit=2, window=3600.0) == 0.0
    assert len(lim._hits[("k",)]) == 1
    assert lim.check(("k",), limit=2, window=3600.0) == 0.0
    assert lim.check(("k",), limit=2, window=3600.0) > 0


def test_refund_gives_back_the_latest_use_only():
    lim = ratelimit.Limiter(clock=lambda: 1.0)
    lim.check(("k",), limit=2, window=60.0)
    lim.check(("k",), limit=2, window=60.0)
    lim.refund(("k",))
    assert lim.check(("k",), limit=2, window=60.0) == 0.0
    lim.refund(("nobody",))                       # unknown key: harmless


# ── Telegram's answers ───────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("exc, off_the_ether", [
    (TelegramForbiddenError(method=None, message="Forbidden: bot was kicked"), True),
    (TelegramBadRequest(method=None, message="Bad Request: chat not found"), True),
    (TelegramBadRequest(method=None, message="Bad Request: bot is not a member"), True),
    (TelegramBadRequest(method=None, message="Bad Request: CHAT_WRITE_FORBIDDEN"), True),
    (TelegramBadRequest(method=None, message="Bad Request: TOPIC_CLOSED"), False),
    (TelegramBadRequest(method=None, message="Bad Request: message is too long"), False),
])
async def test_only_a_chat_that_is_gone_comes_off_the_ether(monkeypatch, exc, off_the_ether):
    dropped = AsyncMock()
    monkeypatch.setattr(relay, "_drop_dead_destination", dropped)
    rt = _rt()
    rt.bot.send_message.side_effect = exc
    _, is_error = await _go(rt)
    assert is_error is True
    assert dropped.await_count == (1 if off_the_ether else 0)


# ── choosing the chat ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_exact_title_beats_a_longer_one_that_contains_it():
    rt = _rt(dests=[{"chat_id": DEST, "title": "Soup"},
                    {"chat_id": DEST - 1, "title": "Soup Chat Two"}])
    text, is_error = await _go(rt, chat_="soup")
    assert (text, is_error) == ("Sent to Soup.", False)
    assert rt.bot.send_message.await_args.args[0] == DEST


@pytest.mark.asyncio
async def test_the_name_asked_for_is_not_rewritten_before_the_lookup():
    rt = _rt()
    await _go(rt, chat_="the @soup chat")
    assert rt.db.fetch.await_args.args[2] == "%the @soup chat%"


@pytest.mark.asyncio
async def test_silenced_chats_are_left_out_of_the_query_itself():
    silenced_chats._cache.update({DEST - 1, DEST - 2})
    rt = _rt()
    await _go(rt)
    sql, *params = rt.db.fetch.await_args.args
    assert "<> ALL($3::bigint[])" in sql
    assert sorted(params[2]) == [DEST - 2, DEST - 1]


# ── after the send, each piece alone ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_failing_track_does_not_hide_the_memory_or_the_log(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("buffer")

    monkeypatch.setattr(relay, "track", boom)
    rt = _rt()
    assert (await _go(rt))[1] is False
    rt.memory.record_message.assert_awaited_once()
    assert rt.activity.log.await_count == 2


@pytest.mark.asyncio
async def test_a_failing_first_log_row_does_not_stop_the_second():
    rt = _rt()
    rt.activity.log.side_effect = [RuntimeError("db"), None]
    assert (await _go(rt))[1] is False
    assert rt.activity.log.await_count == 2


@pytest.mark.asyncio
async def test_what_is_remembered_and_logged_is_what_was_sent():
    rt = _rt()
    await _go(rt, message="the propane is fine")
    sent = rt.bot.send_message.await_args.args[1]
    assert rt.memory.record_message.await_args.kwargs["content"] == sent
    (src, ev, detail), (dst, ev_in, detail_in) = [
        c.args[:3] for c in rt.activity.log.await_args_list]
    assert (src, ev) == (SRC, relay.EVENT_SENT)
    assert "The Soup Chat" in detail and "the propane is fine" in detail
    assert (dst, ev_in) == (DEST, relay.EVENT_RECEIVED)
    assert "Matt" in detail_in and "Poker Night" in detail_in


# ── the words the model is given ─────────────────────────────────────────────

def test_the_guard_rails_are_in_the_words_the_model_is_given():
    from ipedro.capabilities import _RELAY
    desc = relay.tool()["description"]
    for phrase in ("default answer is no", "begged, flattered", "cruel, private or "
                   "embarrassing", "another chat's records", "only once this tool has said so"):
        assert phrase in desc, phrase
    for phrase in ("default answer is no", "Never pass along anything cruel",
                   "Don't say it was sent until the tool says it was",
                   "tuned into the ether"):
        assert phrase in _RELAY, phrase


# ── the reply path ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_anonymous_admin_or_a_channel_is_never_offered_it(monkeypatch):
    for tweak in ("bot", "channel"):
        rt, captured = _tool_rt(monkeypatch)
        _group(rt, ether=True)
        msg = _msg(text="dale please", chat_id=SRC)
        if tweak == "bot":
            msg.from_user.is_bot = True
        else:
            msg.sender_chat = SimpleNamespace(id=-100999, title="Some Channel")
        msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
        await _handler(rt)(msg)
        names = [t["name"] for t in rt.openai.chat_with_tools.await_args.kwargs["tools"]]
        assert "pass_it_along" not in names, tweak


@pytest.mark.asyncio
async def test_a_channel_type_chat_is_never_offered_it(monkeypatch):
    rt, _ = _tool_rt(monkeypatch)
    _group(rt, ether=True)
    msg = _msg(text="dale please", chat_id=SRC)
    msg.chat.type = "channel"
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    names = [t["name"] for t in rt.openai.chat_with_tools.await_args.kwargs["tools"]]
    assert "pass_it_along" not in names


@pytest.mark.asyncio
async def test_a_supergroup_is_offered_it(monkeypatch):
    rt, _ = _tool_rt(monkeypatch)
    _group(rt, ether=True)
    msg = _msg(text="dale please", chat_id=SRC)
    msg.chat.type = "supergroup"
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    names = [t["name"] for t in rt.openai.chat_with_tools.await_args.kwargs["tools"]]
    assert "pass_it_along" in names


@pytest.mark.asyncio
async def test_a_turn_passes_one_message_along_at_most(monkeypatch):
    rt, _ = _tool_rt(monkeypatch)
    _group(rt, ether=True)
    calls = []

    async def fake(rt_, asker, args):
        calls.append(args)
        return "Sent to X.", False

    monkeypatch.setattr(relay, "pass_it_along", fake)
    msg = _msg(text="dale please", chat_id=SRC)
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    run_tool = rt.openai.chat_with_tools.await_args.kwargs["run_tool"]
    first = await run_tool("pass_it_along", {"chat": "a", "message": "1"})
    second = await run_tool("pass_it_along", {"chat": "b", "message": "2"})
    assert first == ("Sent to X.", False)
    assert second[1] is True and "already" in second[0]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_failed_relay_does_not_use_up_the_turns_one(monkeypatch):
    rt, _ = _tool_rt(monkeypatch)
    _group(rt, ether=True)
    results = iter([("No such chat.", True), ("Sent to X.", False)])

    async def fake(rt_, asker, args):
        return next(results)

    monkeypatch.setattr(relay, "pass_it_along", fake)
    msg = _msg(text="dale please", chat_id=SRC)
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    run_tool = rt.openai.chat_with_tools.await_args.kwargs["run_tool"]
    assert (await run_tool("pass_it_along", {}))[1] is True
    assert (await run_tool("pass_it_along", {}))[1] is False


@pytest.mark.asyncio
async def test_if_the_model_goes_quiet_after_a_send_the_asker_still_hears_it(monkeypatch):
    rt, _ = _tool_rt(monkeypatch)
    _group(rt, ether=True)

    async def fake(rt_, asker, args):
        return "Sent to X.", False

    async def loop(messages, *, tools, run_tool, **kw):
        await run_tool("pass_it_along", {"chat": "x", "message": "hi"})
        return None                                   # the next round failed

    monkeypatch.setattr(relay, "pass_it_along", fake)
    rt.openai.chat_with_tools = AsyncMock(side_effect=loop)
    msg = _msg(text="dale please", chat_id=SRC)
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    msg.answer.assert_awaited_once()
    assert msg.answer.await_args.args[0] == "Done."


@pytest.mark.asyncio
async def test_silence_with_nothing_sent_stays_silence(monkeypatch):
    rt, _ = _tool_rt(monkeypatch)
    _group(rt, ether=True)
    rt.openai.chat_with_tools = AsyncMock(return_value=None)
    msg = _msg(text="dale please", chat_id=SRC)
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    msg.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_destinations_hour_is_a_whole_hour(clock):
    rt = _rt()
    for uid, src in ((11, SRC), (12, SRC - 1)):
        assert (await _go(rt, _asker(user_id=uid, chat_id=src)))[1] is False
    clock.now += 3599
    assert (await _go(rt, _asker(user_id=13, chat_id=SRC - 2)))[1] is True
    clock.now += 2
    assert (await _go(rt, _asker(user_id=13, chat_id=SRC - 2)))[1] is False


@pytest.mark.asyncio
async def test_a_chats_hour_is_a_whole_hour(clock):
    rt = _rt()
    for i in range(3):
        rt.db.fetch.return_value = [{"chat_id": DEST - i, "title": "Soup"}]
        assert (await _go(rt, _asker(user_id=20 + i, chat_id=SRC)))[1] is False
    rt.db.fetch.return_value = [{"chat_id": DEST - 9, "title": "Soup"}]
    clock.now += 3599
    assert (await _go(rt, _asker(user_id=30, chat_id=SRC)))[1] is True
    clock.now += 2
    assert (await _go(rt, _asker(user_id=30, chat_id=SRC)))[1] is False


@pytest.mark.asyncio
async def test_an_impersonation_turn_is_never_offered_it(monkeypatch):
    rt, captured = _tool_rt(monkeypatch)
    _group(rt, ether=True)
    monkeypatch.setattr(chat, "resolve_impersonation", AsyncMock(
        return_value=(SimpleNamespace(name="Luke"), ["lol", "ok"])))
    msg = _msg(text="dale act like Luke", chat_id=SRC)
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    rt.openai.chat_with_tools.assert_not_awaited()
    assert "pass_it_along" not in (captured.get("capabilities") or "")
