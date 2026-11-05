"""The brake on the commands that cost money, and the gates on /ether.

/aigen ($0.04 an image), /ether (text-to-speech plus an anonymous voice note
into ANOTHER chat) and /a (the main model) had no limit at all, and /ether
let anyone the bot could hear (a stranger in a DM included) transmit. A
recording of any length was decoded whole, and a transmission to a chat the
bot had been removed from paid for TTS and then vanished.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError

from ipedro import ether, ratelimit
from ipedro.handlers import ai as ai_h
from ipedro.handlers import duckhunt as duck_h
from ipedro.handlers import ether as ether_h
from ipedro.handlers.common import over_limit

ADMIN, MEMBER = 7, 9


@pytest.fixture(autouse=True)
def _fresh_limiter():
    ratelimit.LIMITER.reset()
    yield
    ratelimit.LIMITER.reset()


# ── the limiter ──────────────────────────────────────────────────────────────

def test_a_window_allows_n_then_waits_then_frees_up():
    t = [100.0]
    lim = ratelimit.Limiter(clock=lambda: t[0])
    assert [lim.check("k", limit=2, window=60) for _ in range(2)] == [0.0, 0.0]
    wait = lim.check("k", limit=2, window=60)
    assert 59 <= wait <= 60
    t[0] += 30
    assert 29 <= lim.check("k", limit=2, window=60) <= 30       # still full
    t[0] += 31
    assert lim.check("k", limit=2, window=60) == 0.0            # the first aged out


def test_people_are_counted_separately():
    lim = ratelimit.Limiter()
    assert lim.check(("image", 1), limit=1, window=60) == 0.0
    assert lim.check(("image", 1), limit=1, window=60) > 0
    assert lim.check(("image", 2), limit=1, window=60) == 0.0


def test_idle_keys_are_forgotten():
    t = [0.0]
    lim = ratelimit.Limiter(clock=lambda: t[0])
    for i in range(300):
        lim.check(("k", i), limit=1, window=10)
    t[0] = 1000.0
    for i in range(300, 700):
        lim.check(("k", i), limit=1, window=10)
    assert len(lim._hits) < 700                     # the early ones were pruned


def test_a_chat_wide_refusal_doesnt_burn_the_persons_own_slot():
    uses, _ = ratelimit.LIMITS["ether"]
    cuses, _ = ratelimit.CHAT_LIMITS["ether"]
    for u in range(cuses):                          # fill the chat via many people
        assert ratelimit.wait_for("ether", 1000 + u, chat_id=-5) == 0.0
    assert ratelimit.wait_for("ether", MEMBER, chat_id=-5) > 0   # chat is full
    # MEMBER used nothing, so in another chat they still have their full budget
    for _ in range(uses):
        assert ratelimit.wait_for("ether", MEMBER, chat_id=-6) == 0.0


# ── the commands ─────────────────────────────────────────────────────────────

def _msg(text, *, user_id=MEMBER, chat_type="supergroup", chat_id=-100, voice=None):
    return SimpleNamespace(
        text=text, caption=None, chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=SimpleNamespace(id=user_id), reply_to_message=None, voice=voice,
        reply=AsyncMock(), bot=SimpleNamespace(send_chat_action=AsyncMock()),
        reply_photo=AsyncMock(),
    )


def _rt(*, ether_enabled=True):
    return SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset({ADMIN})),
        chats=SimpleNamespace(get_config=AsyncMock(
            return_value=SimpleNamespace(ether_enabled=ether_enabled))),
        openai=SimpleNamespace(
            generate_image=AsyncMock(return_value=b"PNG"),
            short_completion=AsyncMock(return_value="an answer"),
        ),
        bot=SimpleNamespace(get_file=AsyncMock(), download_file=AsyncMock()),
    )


def _handler(module, rt, name):
    router = module.build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == name)


@pytest.mark.asyncio
async def test_aigen_stops_after_five_an_hour_and_says_when_to_come_back():
    rt = _rt()
    handler = _handler(ai_h, rt, "aigen")
    for _ in range(5):
        await handler(_msg("/aigen a duck"))
    assert rt.openai.generate_image.await_count == 5
    sixth = _msg("/aigen a duck")
    await handler(sixth)
    assert rt.openai.generate_image.await_count == 5            # nothing spent
    said = sixth.reply.await_args.args[0]
    assert "5 images for the hour" in said and "minute" in said


@pytest.mark.asyncio
async def test_a_bot_admin_is_never_limited():
    rt = _rt()
    handler = _handler(ai_h, rt, "aigen")
    for _ in range(12):
        await handler(_msg("/aigen a duck", user_id=ADMIN))
    assert rt.openai.generate_image.await_count == 12


@pytest.mark.asyncio
async def test_a_usage_error_doesnt_use_up_a_slot():
    rt = _rt()
    handler = _handler(ai_h, rt, "aigen")
    for _ in range(20):
        await handler(_msg("/aigen"))                           # no prompt
    await handler(_msg("/aigen a duck"))
    rt.openai.generate_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_ask_has_its_own_larger_budget():
    rt = _rt()
    handler = _handler(ai_h, rt, "quick_ask")
    for _ in range(30):
        await handler(_msg("/a why"))
    assert rt.openai.short_completion.await_count == 30
    await handler(_msg("/a why"))
    assert rt.openai.short_completion.await_count == 30


@pytest.mark.asyncio
async def test_over_limit_with_no_sender_is_a_no():
    msg = _msg("/aigen x")
    msg.from_user = None
    assert await over_limit(_rt(), msg, "image") is False


# ── /ether ───────────────────────────────────────────────────────────────────

def _ether_rt(monkeypatch, *, ether_enabled=True):
    rt = _rt(ether_enabled=ether_enabled)
    rt.db = SimpleNamespace()
    broadcast = AsyncMock(return_value=ether.ManualEtherResult(mode="voice", dest_id=-200))
    monkeypatch.setattr(ether_h.ether, "manual_broadcast", broadcast)
    return rt, broadcast


@pytest.mark.asyncio
async def test_a_chat_that_isnt_on_the_ether_cant_transmit(monkeypatch):
    rt, broadcast = _ether_rt(monkeypatch, ether_enabled=False)
    msg = _msg("/ether hello")
    await _handler(ether_h, rt, "ether_cmd")(msg)
    broadcast.assert_not_awaited()
    assert "isn't tuned into the ether" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_a_stranger_in_a_dm_cant_transmit_but_an_admin_can(monkeypatch):
    rt, broadcast = _ether_rt(monkeypatch)
    dm = _msg("/ether hello", chat_type="private", chat_id=MEMBER)
    await _handler(ether_h, rt, "ether_cmd")(dm)
    broadcast.assert_not_awaited()
    admin_dm = _msg("/ether hello", chat_type="private", chat_id=ADMIN, user_id=ADMIN)
    await _handler(ether_h, rt, "ether_cmd")(admin_dm)
    broadcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_long_text_is_cut_before_it_reaches_text_to_speech(monkeypatch):
    rt, broadcast = _ether_rt(monkeypatch)
    msg = _msg("/ether " + "blah " * 2000)
    await _handler(ether_h, rt, "ether_cmd")(msg)
    sent = broadcast.await_args.kwargs["text"]
    assert len(sent) <= ether_h.MAX_TEXT_CHARS


@pytest.mark.asyncio
@pytest.mark.parametrize("duration,size", [(61, 10_000), (10, 2_000_000)])
async def test_an_overlong_or_oversize_recording_is_refused_before_download(
    monkeypatch, duration, size,
):
    rt, broadcast = _ether_rt(monkeypatch)
    voice = SimpleNamespace(file_id="v", duration=duration, file_size=size)
    msg = _msg("/ether", voice=voice)
    await _handler(ether_h, rt, "ether_cmd")(msg)
    broadcast.assert_not_awaited()
    rt.bot.get_file.assert_not_awaited()              # never downloaded
    assert "too long" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_a_chat_can_only_transmit_ten_times_an_hour_however_many_people(monkeypatch):
    rt, broadcast = _ether_rt(monkeypatch)
    handler = _handler(ether_h, rt, "ether_cmd")
    for u in range(10):
        await handler(_msg("/ether hello", user_id=100 + u))
    assert broadcast.await_count == 10
    await handler(_msg("/ether hello", user_id=999))
    assert broadcast.await_count == 10


# ── a destination that has gone away ────────────────────────────────────────

class _FakeEtherDB:
    def __init__(self, chats):
        self.chats = list(chats)
        self.disabled: list[int] = []

    async def fetch(self, query, *args):
        return [{"chat_id": c} for c in self.chats if c not in self.disabled]

    async def execute(self, query, *args):
        if "ether_enabled = FALSE" in query:
            self.disabled.append(args[0])
        return "OK"


@pytest.mark.asyncio
async def test_a_chat_the_bot_was_removed_from_is_dropped_and_another_tried(monkeypatch):
    """Nothing cleared ether_enabled when the bot was kicked, so the dead
    chat stayed a destination and every transmission to it was paid for
    and lost."""
    monkeypatch.setattr(ether, "apply_radio_effect", AsyncMock(return_value=b"OGG"))
    monkeypatch.setattr(ether.random, "choice", lambda seq: sorted(seq)[0])
    dead = TelegramForbiddenError(method=SimpleNamespace(), message="Forbidden: bot was kicked")
    calls = []

    async def send_voice(dest, *a, **k):
        calls.append(dest)
        if dest == 200:
            raise dead
        return SimpleNamespace(message_id=5)

    bot = SimpleNamespace(
        send_voice=send_voice, send_message=AsyncMock(),
    )
    db = _FakeEtherDB([100, 200, 300])
    res = await ether.manual_broadcast(
        bot, db, SimpleNamespace(text_to_speech=AsyncMock()),
        source_chat_id=100, voice_bytes=b"rec",
    )
    assert calls == [200, 300]
    assert db.disabled == [200]
    assert (res.mode, res.dest_id) == ("voice", 300)


@pytest.mark.asyncio
async def test_if_every_destination_refuses_it_gives_up_cleanly(monkeypatch):
    monkeypatch.setattr(ether, "apply_radio_effect", AsyncMock(return_value=b"OGG"))
    dead = TelegramForbiddenError(method=SimpleNamespace(), message="Forbidden")
    bot = SimpleNamespace(
        send_voice=AsyncMock(side_effect=dead),
        send_message=AsyncMock(side_effect=dead),
    )
    db = _FakeEtherDB([100, 200, 300])
    res = await ether.manual_broadcast(
        bot, db, SimpleNamespace(text_to_speech=AsyncMock()),
        source_chat_id=100, voice_bytes=b"rec",
    )
    assert res.mode == "no_audio"
    assert sorted(db.disabled) == [200, 300]


@pytest.mark.asyncio
async def test_spend_from_these_commands_is_attributed_to_the_chat():
    """/aigen, /aitranslate, /catfact and /beneficiality logged their OpenAI
    spend with chat_id NULL, so per-chat /cost under-reported the priciest."""
    rt = _rt()
    await _handler(ai_h, rt, "aigen")(_msg("/aigen a duck", chat_id=-777))
    assert rt.openai.generate_image.await_args.kwargs["chat_id"] == -777


# ── /duckhunt: a summon, bang, summon loop was a free score farm ─────────────

def _duck_rt(*, active=False):
    spawned = SimpleNamespace(id=1)
    return SimpleNamespace(
        settings=SimpleNamespace(
            admin_ids=frozenset({ADMIN}), duckhunt_duck_lifetime_seconds=3600,
            default_response_policy_group="mention", default_ambient_probability=0.0,
            default_persona="dude", duckhunt_enabled_by_default=True,
            share_photo_enabled_by_default=False,
        ),
        chats=SimpleNamespace(
            get_config=AsyncMock(return_value=SimpleNamespace(duckhunt_enabled=True)),
            upsert_chat=AsyncMock(),
        ),
        duckhunt=SimpleNamespace(
            active_duck=AsyncMock(return_value=active), spawn_duck=AsyncMock(return_value=spawned)),
        openai=SimpleNamespace(cheap_completion=AsyncMock(return_value="(o<  quack")),
    )


def _stub_duck_rendering(monkeypatch):
    monkeypatch.setattr(duck_h, "get_or_create_chat_config",
                        AsyncMock(return_value=SimpleNamespace(duckhunt_enabled=True)))
    monkeypatch.setattr(duck_h, "build_quack_message_for", AsyncMock(return_value="quack"))


def _duck_msg(user_id=MEMBER):
    m = _msg("/duckhunt", user_id=user_id)
    m.answer = AsyncMock()
    m.chat.title = "t"
    return m


@pytest.mark.asyncio
async def test_a_member_can_summon_six_ducks_an_hour_not_seven(monkeypatch):
    _stub_duck_rendering(monkeypatch)
    rt = _duck_rt()
    handler = _handler(duck_h, rt, "duckhunt_cmd")
    for _ in range(6):
        await handler(_duck_msg())
    assert rt.duckhunt.spawn_duck.await_count == 6
    seventh = _duck_msg()
    await handler(seventh)
    assert rt.duckhunt.spawn_duck.await_count == 6                 # nothing spawned
    assert "6 duck summons for the hour" in seventh.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_a_summon_that_finds_a_duck_already_there_costs_nothing(monkeypatch):
    _stub_duck_rendering(monkeypatch)
    rt = _duck_rt(active=True)
    handler = _handler(duck_h, rt, "duckhunt_cmd")
    for _ in range(20):
        await handler(_duck_msg())
    rt.duckhunt.active_duck.return_value = False
    await handler(_duck_msg())
    rt.duckhunt.spawn_duck.assert_awaited_once()                   # slots were never used
