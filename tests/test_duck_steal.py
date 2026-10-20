"""Stealing a duck by answering somebody else's challenge.

A refused bef (or a spooked bang) leaves the duck in the chat and the player
gated behind a challenge. Anyone else may reply to that prompt: the first
correct answer, while the owner's clock still runs, takes the duck. These pin
the rules of that race: who may try, what a try costs, who wins a tie, and
that nothing but a reply to the prompt is ever judged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.duckhunt import service as svc
from ipedro.duckhunt.scoring import ActionOutcome
from ipedro.duckhunt.service import (
    ActiveDuck, DuckhuntService, PendingBefChallenge, StealResult,
)
from ipedro.handlers import duckhunt as dh
from ipedro.handlers.chat import build_router
from tests.test_captcha_intercept import _find_message_handler, _msg, _rt_with

CHAT, OWNER, THIEF = 42, 999, 7
PROMPT = 99
NOW = datetime.now(timezone.utc)


def _challenge(kind="captcha", *, user_id=OWNER, answer="RMY4X", age=5):
    return PendingBefChallenge(
        chat_id=CHAT, user_id=user_id, challenge=answer, kind=kind,
        prompt_message_id=PROMPT, created_at=NOW - timedelta(seconds=age),
    )


def _duck(*, spawned_ago=600, boss=None, id=5):
    return ActiveDuck(
        id=id, chat_id=CHAT, rarity="common",
        spawned_at=NOW - timedelta(seconds=spawned_ago),
        expires_at=NOW + timedelta(hours=1), boss_required_hits=boss,
    )


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    dh._steal_tries.clear()
    dh._PENDING_NAMING.clear()
    monkeypatch.setattr(dh, "track", lambda *a, **k: None)
    yield
    dh._steal_tries.clear()
    dh._PENDING_NAMING.clear()


# ── the service ──────────────────────────────────────────────────────────────

def _service(*, duck=None, executes=("DELETE 1",)):
    row = None
    if duck is not None:
        row = {
            "id": duck.id, "chat_id": duck.chat_id, "rarity": duck.rarity,
            "spawned_at": duck.spawned_at, "expires_at": duck.expires_at,
            "boss_required_hits": duck.boss_required_hits,
            "boss_current_hits": 0,
        }
    db = SimpleNamespace(
        fetchrow=AsyncMock(return_value=row),
        fetchval=AsyncMock(return_value=0),
        execute=AsyncMock(side_effect=list(executes)),
    )
    return DuckhuntService(db), db


@pytest.mark.asyncio
@pytest.mark.parametrize("status, expected", [
    ("DELETE 1", True), ("DELETE 0", False), ("garbage", False),
])
async def test_only_the_caller_that_removed_the_challenge_claims_it(status, expected):
    service, db = _service(executes=(status,))
    assert await service.claim_bef_challenge(CHAT, OWNER, PROMPT) is expected
    sql = db.execute.await_args.args[0]
    assert "prompt_message_id IS NOT DISTINCT FROM $3" in " ".join(sql.split())
    assert db.execute.await_args.args[1:] == (CHAT, OWNER, PROMPT)


@pytest.mark.asyncio
@pytest.mark.parametrize("duck, issued_ago, ok", [
    (_duck(spawned_ago=600), 5, True),          # the duck was there first
    (_duck(spawned_ago=10), 5, True),
    (_duck(spawned_ago=100), 60, True),         # the older duck, a long-open challenge
    (_duck(spawned_ago=2), 60, False),          # a NEW duck since: not the one it was over
    (_duck(spawned_ago=2), 5, False),           # spawned AFTER the challenge
    (_duck(boss=3), 5, False),                  # a boss can't be befriended
    (None, 5, False),                           # nothing there
])
async def test_only_the_duck_a_challenge_was_issued_over_can_be_taken(duck, issued_ago, ok):
    service, _ = _service(duck=duck)
    got = await service.stealable_duck(_challenge(age=issued_ago))
    assert (got is not None) is ok


@pytest.mark.asyncio
async def test_naive_timestamps_do_not_crash_the_comparison():
    naive = _duck(spawned_ago=600)
    naive.spawned_at = naive.spawned_at.replace(tzinfo=None)
    ch = _challenge()
    ch.created_at = ch.created_at.replace(tzinfo=None)
    service, _ = _service(duck=naive)
    assert await service.stealable_duck(ch) is not None


@pytest.mark.asyncio
async def test_the_first_correct_answer_takes_the_duck_and_is_paid():
    service, db = _service(duck=_duck(), executes=("DELETE 1", "UPDATE 1", "INSERT 0 1"))
    res = await service.handle_steal(
        challenge=_challenge(), thief_id=THIEF, thief_name="Luke")
    assert res.status == "stolen" and res.duck.id == 5
    claim, resolve, stats = [c.args for c in db.execute.await_args_list]
    assert "DELETE FROM bef_challenges" in claim[0]
    assert "UPDATE duck_events" in resolve[0] and "resolved = FALSE" in resolve[0]
    assert resolve[1:4] == (THIEF, "bef", res.outcome.points_delta)
    assert "INSERT INTO duck_stats" in stats[0]
    assert stats[1:3] == (CHAT, THIEF)
    assert stats[4] == 0 and stats[5] == 1       # killed 0, befriended 1


@pytest.mark.asyncio
async def test_a_challenge_someone_already_cleared_is_too_late_and_the_duck_untouched():
    service, db = _service(duck=_duck(), executes=("DELETE 0",))
    res = await service.handle_steal(
        challenge=_challenge(), thief_id=THIEF, thief_name="Luke")
    assert res.status == "too_late"
    assert db.execute.await_count == 1                       # never reached the duck


@pytest.mark.asyncio
async def test_a_duck_that_went_in_the_meantime_pays_nothing():
    service, db = _service(duck=_duck(), executes=("DELETE 1", "UPDATE 0"))
    res = await service.handle_steal(
        challenge=_challenge(), thief_id=THIEF, thief_name="Luke")
    assert res.status == "no_duck"
    assert db.execute.await_count == 2                       # no stats row


@pytest.mark.asyncio
async def test_no_duck_means_the_challenge_is_not_even_taken():
    service, db = _service(duck=None)
    res = await service.handle_steal(
        challenge=_challenge(), thief_id=THIEF, thief_name="Luke")
    assert res.status == "no_duck"
    db.execute.assert_not_awaited()


# ── the handler ──────────────────────────────────────────────────────────────

def _rt(*, steal=None, own=None, cooldown=True, duck=True, cheap="PASS: yes"):
    steal = steal or StealResult(
        "stolen", ActionOutcome(True, 1, 1, "You befriended the duck! +1", True),
        _duck())
    return SimpleNamespace(
        db=SimpleNamespace(
            fetchrow=AsyncMock(return_value=None),         # not muted
            fetchval=AsyncMock(return_value="Matt Owner"),
        ),
        settings=SimpleNamespace(duckhunt_action_cooldown_seconds=15),
        duckhunt=SimpleNamespace(
            stealable_duck=AsyncMock(return_value=_duck() if duck else None),
            get_bef_challenge=AsyncMock(return_value=own),
            cooldown_ok=AsyncMock(return_value=cooldown),
            handle_steal=AsyncMock(return_value=steal),
            friend_count=AsyncMock(return_value=2),
        ),
        openai=SimpleNamespace(cheap_chat=AsyncMock(return_value=cheap)),
    )


def _reply_msg(text="RMY4X", *, user_id=THIEF, is_bot=False):
    m = _msg(text=text, user_id=user_id, reply_to=SimpleNamespace(message_id=PROMPT))
    m.from_user.is_bot = is_bot
    m.from_user.first_name = "Luke"
    m.reply = AsyncMock(return_value=SimpleNamespace(message_id=1))
    m.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    return m


@pytest.mark.asyncio
async def test_the_first_right_answer_steals_announces_and_offers_the_naming():
    rt, msg = _rt(), _reply_msg()
    assert await dh.attempt_steal(rt, msg, _challenge()) is True
    kw = rt.duckhunt.handle_steal.await_args.kwargs
    assert kw["thief_id"] == THIEF and kw["challenge"].user_id == OWNER
    said = msg.reply.await_args.args[0]
    assert "Luke" in said and "Matt Owner" in said and "+1" in said
    follow = msg.answer.await_args.args[0]
    assert "Duck #5" in follow or "#5" in follow or "name" in follow.lower()
    assert any(v[0] == THIEF and v[1] == 5 for v in dh._PENDING_NAMING.values())


@pytest.mark.asyncio
async def test_a_wrong_answer_costs_nothing_but_a_try():
    rt, msg = _rt(), _reply_msg(text="WRONG")
    assert await dh.attempt_steal(rt, msg, _challenge()) is True
    rt.duckhunt.handle_steal.assert_not_awaited()
    assert msg.reply.await_args.args[0] == "Not quite."


@pytest.mark.asyncio
async def test_trivia_is_judged_by_the_model_like_the_owners_would_be():
    rt = _rt(cheap="PASS: right")
    msg = _reply_msg(text="the platypus")
    assert await dh.attempt_steal(rt, msg, _challenge("trivia", answer="Which mammal lays eggs?")) is True
    prompt = rt.openai.cheap_chat.await_args.args[0][0]["content"]
    assert "Which mammal lays eggs?" in prompt and "the platypus" in prompt
    rt.duckhunt.handle_steal.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_judge_that_cant_say_is_not_a_win_or_a_loss():
    rt = _rt(cheap=None)
    msg = _reply_msg("the platypus")
    assert await dh.attempt_steal(rt, msg, _challenge("trivia")) is True
    rt.duckhunt.handle_steal.assert_not_awaited()
    assert "Couldn't judge" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_three_tries_a_challenge_then_no_more_model_calls():
    rt = _rt(cheap="FAIL: no")
    ch = _challenge("trivia")
    for _ in range(dh.STEAL_TRIES):
        await dh.attempt_steal(rt, _reply_msg("guess"), ch)
    assert rt.openai.cheap_chat.await_count == dh.STEAL_TRIES
    msg = _reply_msg("guess")
    assert await dh.attempt_steal(rt, msg, ch) is True
    assert rt.openai.cheap_chat.await_count == dh.STEAL_TRIES
    assert "had your tries" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_tries_are_per_person_and_per_challenge():
    rt = _rt(cheap="FAIL: no")
    ch = _challenge("trivia")
    for _ in range(dh.STEAL_TRIES):
        await dh.attempt_steal(rt, _reply_msg("g", user_id=THIEF), ch)
    other = _reply_msg("g", user_id=THIEF + 1)
    await dh.attempt_steal(rt, other, ch)                          # someone else: fresh
    assert rt.openai.cheap_chat.await_count == dh.STEAL_TRIES + 1
    ch2 = _challenge("trivia")
    ch2.prompt_message_id = PROMPT + 1                             # a new challenge: fresh
    await dh.attempt_steal(rt, _reply_msg("g", user_id=THIEF), ch2)
    assert rt.openai.cheap_chat.await_count == dh.STEAL_TRIES + 2


@pytest.mark.asyncio
@pytest.mark.parametrize("why, kwargs, msg_kwargs", [
    ("the owner answering their own", {}, {"user_id": OWNER}),
    ("a bot", {}, {"is_bot": True}),
    ("a muted player", {"muted": True}, {}),
    ("a duck that isn't there", {"duck": False}, {}),
])
async def test_these_are_not_tries_and_fall_through(why, kwargs, msg_kwargs):
    rt = _rt(duck=kwargs.get("duck", True))
    if kwargs.get("muted"):
        rt.db.fetchrow = AsyncMock(return_value={"1": 1})
    msg = _reply_msg(**msg_kwargs)
    assert await dh.attempt_steal(rt, msg, _challenge()) is False, why
    rt.duckhunt.handle_steal.assert_not_awaited()
    msg.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_after_the_owners_clock_nobody_can_take_it():
    rt, msg = _rt(), _reply_msg()
    late = _challenge(age=10_000)
    assert await dh.attempt_steal(rt, msg, late) is False
    rt.duckhunt.handle_steal.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_player_with_a_challenge_of_their_own_clears_that_first():
    rt = _rt(own=_challenge(user_id=THIEF))
    msg = _reply_msg()
    assert await dh.attempt_steal(rt, msg, _challenge()) is True
    rt.duckhunt.handle_steal.assert_not_awaited()
    assert "own challenge" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_taking_a_duck_obeys_the_duck_cooldown():
    rt = _rt(cooldown=False)
    msg = _reply_msg()
    assert await dh.attempt_steal(rt, msg, _challenge()) is True
    rt.duckhunt.handle_steal.assert_not_awaited()
    rt.openai.cheap_chat.assert_not_awaited()
    msg.reply.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status, said", [
    ("too_late", "Too late"), ("no_duck", "already gone"),
])
async def test_losing_the_race_is_said_plainly(status, said):
    rt = _rt(steal=StealResult(status))
    msg = _reply_msg()
    assert await dh.attempt_steal(rt, msg, _challenge()) is True
    assert said in msg.reply.await_args.args[0]
    msg.answer.assert_not_awaited()                      # no naming prompt


@pytest.mark.asyncio
async def test_a_follow_up_that_fails_never_undoes_the_steal():
    rt, msg = _rt(), _reply_msg()
    msg.answer = AsyncMock(side_effect=RuntimeError("telegram"))
    assert await dh.attempt_steal(rt, msg, _challenge()) is True
    rt.duckhunt.handle_steal.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_owners_name_survives_a_failed_lookup():
    rt, msg = _rt(), _reply_msg()
    rt.db.fetchval = AsyncMock(side_effect=RuntimeError("db"))
    assert await dh.attempt_steal(rt, msg, _challenge()) is True
    assert "the other player" in msg.reply.await_args.args[0]


# ── in the chat: only a REPLY to the prompt is ever a try ────────────────────

def _chat_rt(*, by_prompt=None, by_user=None, duckhunt_enabled=True):
    rt = _rt_with(
        find_by_prompt=by_prompt, get_by_user=by_user,
        duckhunt_enabled=duckhunt_enabled,
    )
    rt.settings.duckhunt_action_cooldown_seconds = 15
    rt.db.fetchval = AsyncMock(return_value="Matt Owner")
    rt.duckhunt.stealable_duck = AsyncMock(return_value=_duck())
    rt.duckhunt.cooldown_ok = AsyncMock(return_value=True)
    rt.duckhunt.friend_count = AsyncMock(return_value=0)
    rt.duckhunt.handle_steal = AsyncMock(return_value=StealResult(
        "stolen", ActionOutcome(True, 1, 1, "x", True), _duck()))
    return rt


@pytest.mark.asyncio
async def test_replying_to_someone_elses_prompt_with_the_answer_takes_the_duck():
    rt = _chat_rt(by_prompt=_challenge())
    on_message = _find_message_handler(build_router(rt), "on_message")
    msg = _reply_msg("RMY4X")
    await on_message(msg)
    rt.duckhunt.handle_steal.assert_awaited_once()
    rt.duckhunt.clear_bef_challenge.assert_not_called()      # the owner's gate is
    msg.reply.assert_awaited()                               # taken by the claim


@pytest.mark.asyncio
async def test_plain_chatter_is_never_judged_as_someone_elses_answer():
    """No reply, so the prompt lookup never runs and the sender's own (absent)
    challenge is all that is consulted."""
    rt = _chat_rt(by_prompt=_challenge(), by_user=None)
    on_message = _find_message_handler(build_router(rt), "on_message")
    msg = _msg(text="RMY4X", user_id=THIEF, reply_to=None)
    try:
        await on_message(msg)
    except Exception:
        pass
    rt.duckhunt.find_bef_challenge_by_prompt.assert_not_called()
    rt.duckhunt.handle_steal.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_reply_to_the_prompt_with_duck_hunt_off_is_just_a_reply():
    rt = _chat_rt(by_prompt=_challenge(), duckhunt_enabled=False)
    on_message = _find_message_handler(build_router(rt), "on_message")
    try:
        await on_message(_reply_msg("RMY4X"))
    except Exception:
        pass
    rt.duckhunt.handle_steal.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_reply_when_there_is_nothing_to_steal_carries_on_as_chat():
    rt = _chat_rt(by_prompt=_challenge())
    rt.duckhunt.stealable_duck = AsyncMock(return_value=None)
    on_message = _find_message_handler(build_router(rt), "on_message")
    msg = _reply_msg("RMY4X")
    try:
        await on_message(msg)
    except Exception:
        pass
    rt.duckhunt.handle_steal.assert_not_awaited()
    for call in msg.reply.await_args_list:                   # nothing judged
        assert "Not quite" not in (call.args[0] if call.args else "")


@pytest.mark.asyncio
async def test_the_owner_replying_to_their_own_prompt_is_still_just_their_answer():
    rt = _chat_rt(by_prompt=_challenge(user_id=THIEF))
    on_message = _find_message_handler(build_router(rt), "on_message")
    msg = _reply_msg("RMY4X")
    await on_message(msg)
    rt.duckhunt.handle_steal.assert_not_awaited()
    rt.duckhunt.clear_bef_challenge.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_wrong_steal_is_replied_to_and_does_not_fall_into_chat():
    rt = _chat_rt(by_prompt=_challenge())
    on_message = _find_message_handler(build_router(rt), "on_message")
    msg = _reply_msg("WRONG")
    await on_message(msg)
    assert msg.reply.await_args.args[0] == "Not quite."
    rt.duckhunt.handle_steal.assert_not_awaited()
