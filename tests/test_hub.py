"""The bots hearing each other: publishing, listening, and the loop rules
that stop two bots from talking to each other forever."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import addressed, bot_messages, bots, hub, identity
from ipedro.memory import context_builder

HANK = identity.from_settings(SimpleNamespace(
    bot_name="Hank", bot_aliases="hank, hank hill", bot_flavor="plain",
))
HANK_ID, DALE_ID, CHAT = 7001, 6001, -1001234567890   # a supergroup
BASIC = -4242                                         # a basic group


def _post(**over) -> hub.Post:
    base = dict(
        id=1, chat_id=CHAT, message_id=42, bot_id=DALE_ID,
        bot_username="DaleBot", bot_name="Dale",
        text="hank, the government is in the propane", reply_to_user_id=None,
        depth=0,
    )
    base.update(over)
    return hub.Post(**base)


def _hub() -> hub.Hub:
    return hub.Hub(SimpleNamespace(), bot_id=HANK_ID, bot_username="HankBot",
                   bot_name="Hank")


# ── publishing ───────────────────────────────────────────────────────────────

@pytest.fixture
def published(monkeypatch):
    h = _hub()
    h.publish = AsyncMock()
    monkeypatch.setattr(hub, "_hub", h)
    return h.publish


@pytest.mark.asyncio
async def test_track_publishes_group_lines_with_their_depth(published):
    bot_messages.track(CHAT, 77, "sh-sha", replied_to_user_id=DALE_ID, hub_depth=2)
    await asyncio.sleep(0)
    published.assert_awaited_once_with(
        CHAT, 77, "sh-sha", reply_to_user_id=DALE_ID, depth=2,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_id,message_id,text", [
    (12345, 77, "a DM: no other bot is in it"),
    (CHAT, None, "the send failed"),
    (CHAT, 77, None),                       # a photo with no caption
    (CHAT, 77, ""),
])
async def test_nothing_to_publish(published, chat_id, message_id, text):
    hub.publish_soon(chat_id, message_id, text)
    await asyncio.sleep(0)
    published.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_hub_means_no_publishing(monkeypatch):
    monkeypatch.setattr(hub, "_hub", None)
    hub.publish_soon(CHAT, 77, "hello")     # must simply not blow up


@pytest.mark.asyncio
async def test_a_failed_publish_never_reaches_the_sender(published):
    published.side_effect = RuntimeError("hub down")
    bot_messages.track(CHAT, 77, "hello")
    await asyncio.sleep(0)
    published.assert_awaited_once()         # tried, failed, swallowed


@pytest.mark.asyncio
async def test_publish_is_one_insert_and_notify():
    db = SimpleNamespace(execute=AsyncMock())
    h = hub.Hub(db, bot_id=HANK_ID, bot_username="HankBot", bot_name="Hank")
    await h.publish(CHAT, 9, "x" * 5000, reply_to_user_id=DALE_ID, depth=1)
    sql, *args = db.execute.await_args.args
    assert "INSERT INTO bot_posts" in sql and "pg_notify('bot_posts'" in sql
    assert args[:5] == [CHAT, 9, HANK_ID, "HankBot", "Hank"]
    assert len(args[5]) == 4096
    assert args[6:] == [DALE_ID, 1]


def test_child_bots_are_pointed_at_the_hub():
    row = bots.BotRow(id=1, telegram_id=HANK_ID, username="HankBot", name="Hank",
                      aliases="hank", status="active",
                      token="7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1")
    env = bots.child_env({"HUB_DATABASE_URL": "postgresql://evil/x"}, row,
                         "postgresql://u:p@postgres:5432/ipedro")
    assert env["HUB_DATABASE_URL"] == "postgresql://u:p@postgres:5432/ipedro"


# ── hearing another bot ──────────────────────────────────────────────────────

@pytest.fixture
def heard(monkeypatch):
    """A Hank deployment hearing posts, with the model and Telegram faked."""
    cfg = SimpleNamespace(
        memory_enabled=True, response_policy="mention", persona="dude",
        persona_custom=None,
    )
    state = SimpleNamespace(cfg=cfg, tracked=[], flags=set(), briefs=[])
    rt = SimpleNamespace(
        chats=SimpleNamespace(get_config=AsyncMock(return_value=cfg)),
        users=SimpleNamespace(upsert_user=AsyncMock()),
        memory=SimpleNamespace(record_message=AsyncMock()),
        openai=SimpleNamespace(chat=AsyncMock(return_value="propane's clean, dale")),
        bot=SimpleNamespace(send_message=AsyncMock(
            return_value=SimpleNamespace(message_id=555))),
        activity=SimpleNamespace(log=AsyncMock()),
        settings=SimpleNamespace(), db=SimpleNamespace(),
    )

    async def fake_build(**kw):
        return context_builder.BuiltContext(
            messages=[{"role": "user", "content": kw["latest_user_text"]}],
            tokens=1,
        )

    async def has_flag(db, chat_id, user_id, flag):
        return (chat_id, user_id, flag) in state.flags

    def brief(cfg, **kw):
        state.briefs.append(kw)
        return "what you can do"

    monkeypatch.setattr(context_builder, "build_context", fake_build)
    monkeypatch.setattr("ipedro.capabilities.capability_brief", brief)
    monkeypatch.setattr("ipedro.user_flags.has_flag", has_flag)
    monkeypatch.setattr(
        bot_messages, "track",
        lambda *a, **k: state.tracked.append((a, k)),
    )
    state.rt, state.hub = rt, _hub()
    hub._left_until.clear()

    async def hear(post):
        await hub.handle_post(rt, HANK, state.hub, post)

    state.hear = hear
    return state


@pytest.mark.asyncio
async def test_a_chat_this_bot_isnt_in_is_ignored(heard):
    heard.rt.chats.get_config.return_value = None
    await heard.hear(_post())
    heard.rt.memory.record_message.assert_not_awaited()
    heard.rt.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_another_bots_line_becomes_part_of_the_conversation(heard):
    await heard.hear(_post(text="the propane's fine, nothing to see"))
    heard.rt.users.upsert_user.assert_awaited_once_with(
        DALE_ID, "DaleBot", "Dale", None, True,
    )
    heard.rt.memory.record_message.assert_awaited_once_with(
        chat_id=CHAT, role="user", content="the propane's fine, nothing to see",
        message_id=42, user_id=DALE_ID,
    )
    heard.rt.bot.send_message.assert_not_awaited()     # not meant for Hank


@pytest.mark.asyncio
async def test_named_it_answers_as_a_reply_one_level_deeper(heard):
    await heard.hear(_post(depth=1))
    send = heard.rt.bot.send_message.await_args
    assert send.args == (CHAT, "propane's clean, dale")
    assert send.kwargs["reply_parameters"].message_id == 42
    (args, kw), = heard.tracked
    assert args == (CHAT, 555, "propane's clean, dale")
    assert kw == {"replied_to_user_id": DALE_ID, "hub_depth": 2}
    assert heard.rt.memory.record_message.await_args.kwargs["role"] == "assistant"
    assert "answered Dale" in heard.rt.activity.log.await_args.args[2]
    assert heard.briefs == [{"dale_flavor": False}]     # briefed as itself


@pytest.mark.asyncio
async def test_a_reply_to_it_counts_as_being_addressed(heard):
    await heard.hear(_post(text="no.", reply_to_user_id=HANK_ID))
    heard.rt.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_chain_stops_at_max_depth(heard):
    await heard.hear(_post(depth=hub.MAX_DEPTH))
    heard.rt.memory.record_message.assert_awaited_once()   # still heard it
    heard.rt.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_bot_triggered_reply_per_gap_per_chat(heard):
    await heard.hear(_post(id=1))
    await heard.hear(_post(id=2, message_id=43))
    assert heard.rt.bot.send_message.await_count == 1
    heard.hub._last_bot_reply[CHAT] -= hub.MIN_GAP_SECONDS
    await heard.hear(_post(id=3, message_id=44))
    assert heard.rt.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_a_commands_only_chat_gets_no_bot_triggered_reply(heard):
    heard.cfg.response_policy = "commands"
    await heard.hear(_post())
    heard.rt.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_shushed_bot_is_ignored_here_too(heard):
    heard.flags.add((CHAT, DALE_ID, "shutup"))
    await heard.hear(_post())
    heard.rt.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_off_still_answers_but_keeps_nothing(heard):
    heard.cfg.memory_enabled = False
    await heard.hear(_post())
    heard.rt.memory.record_message.assert_not_awaited()
    heard.rt.bot.send_message.assert_awaited_once()


# ── the listener ─────────────────────────────────────────────────────────────

class FakeConn:
    def __init__(self, payloads):
        self.payloads = payloads
        self.execute = AsyncMock()
        self.close = AsyncMock()

    async def add_listener(self, channel, cb):
        assert channel == hub.CHANNEL
        loop = asyncio.get_running_loop()
        for p in self.payloads:
            loop.call_soon(cb, self, 1, channel, p)


@pytest.mark.asyncio
async def test_listen_delivers_other_bots_posts_and_skips_its_own(monkeypatch):
    h = _hub()
    posts = {"1": _post(id=1), "2": _post(id=2, bot_id=HANK_ID), "3": _post(id=3)}
    h.fetch = AsyncMock(side_effect=lambda pid: posts[str(pid)])
    stop = asyncio.Event()
    got = []

    async def on_post(post):
        got.append(post.id)
        if post.id == 3:
            stop.set()

    async def connect(dsn):
        return FakeConn(["1", "2", "3"])

    await asyncio.wait_for(h.listen("dsn", on_post, stop, connect=connect), 5)
    assert got == [1, 3]


@pytest.mark.asyncio
async def test_listen_reconnects_after_a_failed_connect(monkeypatch):
    monkeypatch.setattr(hub, "_sleep", AsyncMock())
    h = _hub()
    h.fetch = AsyncMock(return_value=_post())
    stop = asyncio.Event()
    attempts = []

    async def connect(dsn):
        attempts.append(dsn)
        if len(attempts) == 1:
            raise OSError("db restarting")
        return FakeConn(["1"])

    async def on_post(post):
        stop.set()

    await asyncio.wait_for(h.listen("dsn", on_post, stop, connect=connect), 5)
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_a_bad_post_doesnt_kill_the_listener():
    h = _hub()
    h.fetch = AsyncMock(side_effect=[_post(id=1), _post(id=2)])
    stop = asyncio.Event()
    seen = []

    async def on_post(post):
        seen.append(post.id)
        if post.id == 1:
            raise RuntimeError("model fell over")
        stop.set()

    async def connect(dsn):
        return FakeConn(["1", "2"])

    await asyncio.wait_for(h.listen("dsn", on_post, stop, connect=connect), 5)
    assert seen == [1, 2]


# ── basic groups: another bot's message id is from ITS sequence ─────────────

def test_only_supergroups_and_channels_share_message_ids():
    assert hub.shares_message_ids(-1001234567890)
    assert hub.shares_message_ids(-1009999999999)
    assert not hub.shares_message_ids(-4242)
    assert not hub.shares_message_ids(-999999999999)


@pytest.mark.asyncio
async def test_in_a_basic_group_the_other_bots_id_is_not_used_as_a_key_or_quoted(heard):
    """Its message_id would collide with one of ours: the messages table's
    dedupe kept the old row and the embedding upsert overwrote the old
    message's embedding with the other bot's text."""
    await heard.hear(_post(chat_id=BASIC, depth=1))
    recorded = heard.rt.memory.record_message.await_args_list
    assert recorded[0].kwargs["message_id"] is None
    send = heard.rt.bot.send_message.await_args
    assert "reply_parameters" not in send.kwargs      # it would quote the wrong message


# ── the chat's own rule decides ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_reply_only_chat_does_not_answer_being_named(heard):
    heard.cfg.response_policy = "reply"
    await heard.hear(_post())                                   # named, not a reply
    heard.rt.bot.send_message.assert_not_awaited()
    await heard.hear(_post(id=2, message_id=43, text="no.", reply_to_user_id=HANK_ID))
    heard.rt.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_chat_it_was_removed_from_is_not_a_crash(heard):
    from aiogram.exceptions import TelegramForbiddenError
    heard.rt.bot.send_message = AsyncMock(side_effect=TelegramForbiddenError(
        method=SimpleNamespace(), message="Forbidden: bot was kicked from the group chat"))
    await heard.hear(_post())                                   # must not raise
    assert heard.tracked == []


# ── the listener's failure modes ─────────────────────────────────────────────

class _FailingListen(FakeConn):
    async def add_listener(self, channel, cb):
        raise OSError("LISTEN refused")


@pytest.mark.asyncio
async def test_a_failing_listen_reconnects_instead_of_ending_the_hub(monkeypatch):
    """add_listener sat outside the try, so one failure there ended the
    hub for the life of the process and leaked the connection."""
    monkeypatch.setattr(hub, "_sleep", AsyncMock())
    h = _hub()
    h.fetch = AsyncMock(return_value=_post())
    stop = asyncio.Event()
    conns = []

    async def connect(dsn):
        conn = _FailingListen([]) if not conns else FakeConn(["1"])
        conns.append(conn)
        return conn

    async def on_post(post):
        stop.set()

    await asyncio.wait_for(h.listen("dsn", on_post, stop, connect=connect), 5)
    assert len(conns) == 2
    conns[0].close.assert_awaited_once()                       # not leaked


@pytest.mark.asyncio
async def test_a_slow_reply_does_not_hold_up_the_next_post():
    """handle_post is several seconds of model call. Awaited inline it
    froze delivery of every other post and the keepalive."""
    h = _hub()
    posts = {"1": _post(id=1), "2": _post(id=2, message_id=43)}
    h.fetch = AsyncMock(side_effect=lambda pid: posts[str(pid)])
    stop = asyncio.Event()
    gate = asyncio.Event()
    order = []

    async def on_post(post):
        if post.id == 1:
            await gate.wait()                  # the first one is slow
        order.append(post.id)
        if post.id == 2:
            gate.set()
            stop.set()

    async def connect(dsn):
        return FakeConn(["1", "2"])

    await asyncio.wait_for(h.listen("dsn", on_post, stop, connect=connect), 5)
    assert order[0] == 2                       # 2 was not stuck behind 1


@pytest.mark.asyncio
async def test_run_keeps_trying_when_startup_fails_once(monkeypatch):
    """One hiccup at startup left the bot deaf and mute on the hub until
    its next restart, with everything else running fine."""
    from ipedro.db import pool
    monkeypatch.setattr(hub, "_sleep", AsyncMock())
    attempts = []

    class _DB:
        async def close(self):
            pass

    async def connect(dsn, **kw):
        attempts.append(kw)
        if len(attempts) == 1:
            raise OSError("postgres is still starting")
        return _DB()

    monkeypatch.setattr(pool.Database, "connect", staticmethod(connect))
    stop = asyncio.Event()

    async def listen(self, dsn, on_post, stop_, **kw):
        stop_.set()

    monkeypatch.setattr(hub.Hub, "listen", listen)
    rt = SimpleNamespace(bot=SimpleNamespace(
        me=AsyncMock(return_value=SimpleNamespace(id=HANK_ID, username="HankBot"))))
    settings = SimpleNamespace(database_url="postgresql://x/y", hub_database_url=None,
                               bot_name="Hank", bot_aliases="hank", bot_flavor="plain",
                               manages_bots=False)
    await asyncio.wait_for(hub.run(rt, settings, stop), 5)
    assert len(attempts) == 2
    assert attempts[-1]["max_size"] == 1           # one pooled connection


@pytest.mark.asyncio
async def test_a_chat_that_kicked_us_stops_costing_model_calls(heard):
    """Dale was removed from the group but its config row remains; Hank keeps
    saying "dale". Each line used to build a context, call the model and fail
    to send. The first failure is enough to know."""
    from aiogram.exceptions import TelegramForbiddenError

    heard.rt.bot.send_message.side_effect = TelegramForbiddenError(
        method=None, message="bot was kicked from the group chat")
    await heard.hear(_post(id=1))
    assert heard.rt.openai.chat.await_count == 1
    heard.hub._last_bot_reply[CHAT] -= hub.MIN_GAP_SECONDS
    await heard.hear(_post(id=2, message_id=43))
    await heard.hear(_post(id=3, message_id=44))
    assert heard.rt.openai.chat.await_count == 1               # nothing more spent
    heard.rt.memory.record_message.assert_awaited_once()         # nor re-recorded
    # ...until the hour is up, in case it was let back in
    hub._left_until[CHAT] = hub.time.monotonic() - 1
    heard.hub._last_bot_reply[CHAT] -= hub.MIN_GAP_SECONDS
    await heard.hear(_post(id=4, message_id=45))
    assert heard.rt.openai.chat.await_count == 2


@pytest.mark.asyncio
async def test_a_one_off_send_error_is_not_mistaken_for_being_kicked(heard):
    from aiogram.exceptions import TelegramBadRequest

    heard.rt.bot.send_message.side_effect = TelegramBadRequest(
        method=None, message="message text is empty")
    await heard.hear(_post(id=1))
    heard.hub._last_bot_reply[CHAT] -= hub.MIN_GAP_SECONDS
    await heard.hear(_post(id=2, message_id=43))
    assert heard.rt.openai.chat.await_count == 2


def test_answering_another_bot_does_not_open_the_human_follow_up_window():
    """Dale answers a human and names Hank; Hank answers Dale through the hub.
    Hank's reply used to open Hank's follow-up window too, so the human's next
    "why?" drew an answer from BOTH bots."""
    addressed.reset()
    bot_messages.track(CHAT, 90, "hank, you're up", hub_depth=1,
                       replied_to_user_id=DALE_ID)
    assert not addressed.in_conversation(CHAT)
    bot_messages.track(CHAT, 91, "sh-sha, the human spoke to me", hub_depth=0)
    assert addressed.in_conversation(CHAT)


@pytest.mark.asyncio
async def test_the_listening_connection_has_a_command_timeout(monkeypatch):
    """Its keepalive is a command; with no timeout a silently dropped
    connection stalled it for the kernel's TCP timeout."""
    import asyncpg

    seen = {}

    async def fake_connect(dsn, **kw):
        seen.update(kw)
        return "conn"

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    assert await hub._connect_listener("postgresql://x/y") == "conn"
    assert seen["command_timeout"] == hub._LISTEN_COMMAND_TIMEOUT <= 30
