"""Stealing a duck by answering somebody else's challenge.

A refused bef (or a spooked bang) leaves the duck in the chat and the player
gated behind a challenge. Anyone else may reply to that prompt: the first
correct answer, while the owner's clock still runs, takes the duck. These pin
the rules of that race: who may try, what a try costs, who wins a tie, and
that nothing but a reply to the prompt is ever judged.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import prompts
from ipedro.duckhunt.scoring import ActionOutcome
from ipedro.duckhunt.service import (
    ActiveDuck, DuckhuntService, PendingBefChallenge, StealResult,
)
from ipedro.handlers import duckhunt as dh
from ipedro.handlers.chat import build_router
from tests.test_captcha_intercept import _find_message_handler, _msg, _rt_with

CHAT, OWNER, THIEF = 42, 999, 7
PROMPT, DUCK = 99, 5
NOW = datetime.now(timezone.utc)


def _challenge(kind="captcha", *, user_id=OWNER, answer="RMY4X", age=5,
               duck_id=DUCK, prompt=PROMPT):
    return PendingBefChallenge(
        chat_id=CHAT, user_id=user_id, challenge=answer, kind=kind,
        prompt_message_id=prompt, created_at=NOW - timedelta(seconds=age),
        duck_id=duck_id,
    )


def _duck(*, boss=None, id=DUCK, spawned_ago=600):
    return ActiveDuck(
        id=id, chat_id=CHAT, rarity="common",
        spawned_at=NOW - timedelta(seconds=spawned_ago),
        expires_at=NOW + timedelta(hours=1), boss_required_hits=boss,
    )


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    dh._steal_tries.clear()
    dh._PENDING_NAMING.clear()
    tracked = []
    monkeypatch.setattr(dh, "track", lambda *a, **k: tracked.append(a))
    yield tracked
    dh._steal_tries.clear()
    dh._PENDING_NAMING.clear()


# ── the service, against scripted statements ─────────────────────────────────

def _row(duck):
    return None if duck is None else {
        "id": duck.id, "chat_id": duck.chat_id, "rarity": duck.rarity,
        "spawned_at": duck.spawned_at, "expires_at": duck.expires_at,
        "boss_required_hits": duck.boss_required_hits, "boss_current_hits": 0,
    }


def _service(*, duck=None, current="same", executes=("UPDATE 1", "DELETE 1", "INSERT 0 1")):
    """`current` is the owner's challenge row as the database has it now
    ("same" = the one being answered, None = already gone, or another)."""
    if current == "same":
        current = _challenge()

    async def fetchrow(sql, *args):
        if "FROM duck_events" in sql:
            return _row(duck)
        if "FROM bef_challenges" in sql:
            return None if current is None else {
                "chat_id": current.chat_id, "user_id": current.user_id,
                "challenge": current.challenge, "kind": current.kind,
                "prompt_message_id": current.prompt_message_id,
                "created_at": current.created_at, "duck_id": current.duck_id,
            }
        return None

    db = SimpleNamespace(
        fetchrow=AsyncMock(side_effect=fetchrow),
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
@pytest.mark.parametrize("duck, challenge_duck, ok", [
    (_duck(id=5), 5, True),                   # the duck it was issued over
    (_duck(id=6), 5, False),                  # a different duck is there now
    (_duck(id=5), None, False),               # an admin's forced challenge
    (_duck(id=5, boss=3), 5, False),          # a boss can't be befriended
    (None, 5, False),                         # nothing there
])
async def test_only_the_duck_a_challenge_was_issued_over_can_be_taken(duck, challenge_duck, ok):
    service, db = _service(duck=duck)
    got = await service.stealable_duck(_challenge(duck_id=challenge_duck))
    assert (got is not None) is ok
    if challenge_duck is None:
        db.fetchrow.assert_not_awaited()          # nothing to compare, nothing to ask


@pytest.mark.asyncio
async def test_stealable_duck_looks_in_the_challenges_chat():
    service, db = _service(duck=_duck())
    await service.stealable_duck(_challenge())
    assert db.fetchrow.await_args.args[1] == CHAT


@pytest.mark.asyncio
async def test_the_first_correct_answer_takes_that_duck_for_the_thief():
    service, db = _service(duck=_duck(id=5))
    res = await service.handle_steal(
        challenge=_challenge(), thief_id=THIEF, thief_name="Luke")
    assert res.status == "stolen" and res.duck.id == 5
    resolve, claim, stats = [c.args for c in db.execute.await_args_list]
    # the duck first, THEN the owner's challenge: a lost duck keeps the gate
    assert "UPDATE duck_events" in resolve[0] and "resolved = FALSE" in resolve[0]
    assert resolve[1:] == (THIEF, "bef", 1, 5)
    assert "DELETE FROM bef_challenges" in claim[0]
    assert claim[1:] == (CHAT, OWNER, PROMPT)     # the OWNER's row for THIS prompt
    assert "INSERT INTO duck_stats" in stats[0]
    assert stats[1:4] == (CHAT, THIEF, "Luke")   # the thief's row, under their name
    assert stats[4] == 0 and stats[5] == 1       # killed 0, befriended 1
    assert stats[8] == 1 and stats[9] == 1       # one point, one streak
    assert res.outcome.points_delta == 1


@pytest.mark.asyncio
async def test_a_duck_lost_in_the_meantime_leaves_the_owners_challenge_alone():
    """Used to delete the owner's challenge first and then lose the duck,
    leaving the owner un-gated for nothing."""
    service, db = _service(duck=_duck(), executes=("UPDATE 0",))
    res = await service.handle_steal(
        challenge=_challenge(), thief_id=THIEF, thief_name="Luke")
    assert res.status == "no_duck"
    assert db.execute.await_count == 1                   # only the duck update
    assert "UPDATE duck_events" in db.execute.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [
    None,                                                # cleared
    _challenge(prompt=PROMPT + 1),                       # replaced by a newer one
])
async def test_a_challenge_that_is_no_longer_the_one_answered_is_too_late(current):
    service, db = _service(duck=_duck(), current=current)
    res = await service.handle_steal(
        challenge=_challenge(), thief_id=THIEF, thief_name="Luke")
    assert res.status == "too_late"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_challenge_cleared_after_the_duck_is_won_does_not_undo_the_win():
    service, db = _service(duck=_duck(), executes=("UPDATE 1", "DELETE 0", "INSERT 0 1"))
    res = await service.handle_steal(
        challenge=_challenge(), thief_id=THIEF, thief_name="Luke")
    assert res.status == "stolen" and db.execute.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("duck", [None, _duck(boss=3), _duck(id=6)])
async def test_handle_steal_rechecks_the_duck_after_the_judge(duck):
    """attempt_steal looks BEFORE the slow model call; a boss or a new duck
    can turn up during it. handle_steal must not trust the earlier look."""
    service, db = _service(duck=duck)
    res = await service.handle_steal(
        challenge=_challenge(), thief_id=THIEF, thief_name="Luke")
    assert res.status == "no_duck"
    db.execute.assert_not_awaited()


# ── the handler ──────────────────────────────────────────────────────────────

def _rt(*, steal=None, own=None, cooldown=True, duck=True, cheap="PASS: yes",
        current="same"):
    steal = steal or StealResult(
        "stolen", ActionOutcome(True, 1, 1, "You befriended the duck! +1", True),
        _duck())

    async def get_bef_challenge(chat_id, user_id):
        if user_id == OWNER:                       # the challenge being answered
            return _challenge() if current == "same" else current
        return own if user_id == THIEF else None   # a thief's challenge of their own

    return SimpleNamespace(
        db=SimpleNamespace(
            fetchrow=AsyncMock(return_value=None),         # not muted
            fetchval=AsyncMock(return_value="Matt Owner"),
        ),
        settings=SimpleNamespace(duckhunt_action_cooldown_seconds=15),
        duckhunt=SimpleNamespace(
            stealable_duck=AsyncMock(return_value=_duck() if duck else None),
            get_bef_challenge=AsyncMock(side_effect=get_bef_challenge),
            clear_bef_challenge=AsyncMock(),
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
async def test_the_win_is_credited_announced_and_named_for_the_thief(_fresh):
    rt, msg = _rt(), _reply_msg()
    assert await dh.attempt_steal(rt, msg, _challenge()) is True
    kw = rt.duckhunt.handle_steal.await_args.kwargs
    assert kw["thief_id"] == THIEF and kw["thief_name"] == "Luke"
    assert kw["challenge"].user_id == OWNER
    assert msg.reply.await_args.args[0] == (
        "🦆 Luke got the answer in first, so Matt Owner's duck is Luke's now. +1")
    assert rt.db.fetchval.await_args.args[1] == OWNER      # the victim is the owner
    rt.duckhunt.friend_count.assert_awaited_once_with(CHAT, THIEF)
    follow = msg.answer.await_args.args[0]
    assert "/duckname 5 " in follow                          # names duck 5
    assert dh._PENDING_NAMING[(CHAT, 2)][:2] == (THIEF, 5)  # the thief may name it
    assert [c[:2] for c in _fresh] == [(CHAT, 1), (CHAT, 2)]
    assert _fresh[1][2] == follow


@pytest.mark.asyncio
@pytest.mark.parametrize("count, shout", [
    (1, "First feathered friend"), (5, "5-duck club"),
])
async def test_the_friend_count_is_read_after_the_resolve(count, shout):
    """friend_count counts resolved 'bef' ducks and this one is already
    resolved, so it is used as is (the normal bef path reads it BEFORE and
    adds one)."""
    rt, msg = _rt(), _reply_msg()
    rt.duckhunt.friend_count = AsyncMock(return_value=count)
    await dh.attempt_steal(rt, msg, _challenge())
    assert shout in msg.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_a_wrong_answer_costs_nothing_but_a_try():
    rt, msg = _rt(), _reply_msg(text="WRONG")
    assert await dh.attempt_steal(rt, msg, _challenge()) is True
    rt.duckhunt.handle_steal.assert_not_awaited()
    assert msg.reply.await_args.args[0] == "Not quite."


@pytest.mark.asyncio
async def test_a_wrong_answer_never_gets_the_judges_own_words():
    """That line is read by everyone still racing; it must not explain."""
    rt = _rt(cheap="FAIL: it was the platypus, silly")
    msg = _reply_msg("the echidna")
    await dh.attempt_steal(rt, msg, _challenge("trivia", answer="Which mammal lays eggs?"))
    assert msg.reply.await_args.args[0] == "Not quite."


@pytest.mark.asyncio
async def test_a_steal_is_judged_strictly_and_the_owners_retry_generously():
    ch = _challenge("trivia", answer="Which mammal lays eggs?")
    rt = _rt(cheap="PASS: right")
    await dh.attempt_steal(rt, _reply_msg("the platypus"), ch)
    sent = rt.openai.cheap_chat.await_args.args[0][0]["content"]
    assert "Be STRICT" in sent and "the platypus" in sent
    assert "Which mammal lays eggs?" in sent
    assert "Be GENEROUS" not in sent

    rt2 = _rt(cheap="PASS: ok")
    await dh.judge_challenge(rt2, ch, "platypus-ish")
    owner_sent = rt2.openai.cheap_chat.await_args.args[0][0]["content"]
    assert "Be GENEROUS" in owner_sent and "Be STRICT" not in owner_sent


def test_the_strict_prompt_keeps_the_guards_the_generous_one_has():
    p = prompts.DUCK_STEAL_JUDGE_PROMPT
    assert "{challenge}" in p and "{answer}" in p
    assert "quoted as data" in p and "instruction" in p
    assert "WITHOUT saying or hinting at the right answer" in p
    assert p.format(challenge="c", answer="a")        # no stray placeholders


@pytest.mark.asyncio
async def test_a_captcha_must_match_exactly():
    rt = _rt()
    assert await dh.attempt_steal(rt, _reply_msg("rmy4x "), _challenge(answer="RMY4X")) is True
    rt.duckhunt.handle_steal.assert_awaited_once()          # case/space tolerant
    rt2 = _rt()
    await dh.attempt_steal(rt2, _reply_msg("RMY4Y"), _challenge(answer="RMY4X"))
    rt2.duckhunt.handle_steal.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_judge_that_cant_say_is_not_a_win_a_loss_or_a_try():
    rt = _rt(cheap=None)
    ch = _challenge("trivia")
    for _ in range(dh.STEAL_TRIES + 2):
        msg = _reply_msg("the platypus")
        assert await dh.attempt_steal(rt, msg, ch) is True
        assert "Couldn't judge" in msg.reply.await_args.args[0]
    rt.duckhunt.handle_steal.assert_not_awaited()
    assert rt.openai.cheap_chat.await_count == dh.STEAL_TRIES + 2   # never "out of tries"


def test_three_tries_is_the_documented_number():
    assert dh.STEAL_TRIES == 3


@pytest.mark.asyncio
async def test_three_tries_a_challenge_then_no_more_model_calls():
    rt = _rt(cheap="FAIL: no")
    ch = _challenge("trivia")
    for _ in range(3):
        await dh.attempt_steal(rt, _reply_msg("guess"), ch)
    assert rt.openai.cheap_chat.await_count == 3
    msg = _reply_msg("guess")
    assert await dh.attempt_steal(rt, msg, ch) is True
    assert rt.openai.cheap_chat.await_count == 3
    assert "had your tries" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_tries_are_per_person_per_challenge_and_per_chat():
    rt = _rt(cheap="FAIL: no")
    ch = _challenge("trivia")
    for _ in range(3):
        await dh.attempt_steal(rt, _reply_msg("g", user_id=THIEF), ch)
    await dh.attempt_steal(rt, _reply_msg("g", user_id=THIEF + 1), ch)    # someone else
    assert rt.openai.cheap_chat.await_count == 4
    newer = _challenge("trivia", prompt=PROMPT + 1)
    rt.duckhunt.get_bef_challenge = AsyncMock(side_effect=lambda c, u: newer if u == OWNER else None)
    await dh.attempt_steal(rt, _reply_msg("g"), newer)
    assert rt.openai.cheap_chat.await_count == 5                          # a new challenge
    other = PendingBefChallenge(
        chat_id=CHAT + 1, user_id=OWNER, challenge="q", kind="trivia",
        prompt_message_id=PROMPT, created_at=ch.created_at, duck_id=DUCK)
    m = _reply_msg("g")
    m.chat.id = CHAT + 1
    rt.duckhunt.get_bef_challenge = AsyncMock(side_effect=lambda c, u: other if u == OWNER else None)
    await dh.attempt_steal(rt, m, other)                                  # Telegram ids are per chat
    assert rt.openai.cheap_chat.await_count == 6


def test_the_tries_table_is_bounded_and_small_leaves_it_alone():
    for _ in range(dh.STEAL_TRIES):
        assert dh._spend_steal_try(CHAT, PROMPT, THIEF) is True
    for i in range(50):
        dh._spend_steal_try(CHAT, 1000 + i, THIEF + 1)
    assert dh._spend_steal_try(CHAT, PROMPT, THIEF) is False
    for i in range(2500):
        dh._spend_steal_try(CHAT, 5000 + i, THIEF)
    assert len(dh._steal_tries) <= 2001


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
async def test_every_gate_is_asked_about_the_thief_not_the_owner(monkeypatch):
    rt, msg = _rt(), _reply_msg()
    seen = []
    monkeypatch.setattr(
        dh, "debug_is_on", lambda uid, name: seen.append((uid, name)) or False)
    assert await dh.attempt_steal(rt, msg, _challenge()) is True
    assert rt.db.fetchrow.await_args.args[1:] == (CHAT, THIEF, "shutup")   # has_flag
    asked = [c.args for c in rt.duckhunt.get_bef_challenge.await_args_list]
    assert (CHAT, THIEF) in asked                                          # own challenge
    rt.duckhunt.cooldown_ok.assert_awaited_once_with(CHAT, THIEF, 15)
    assert (THIEF, "bypass_cooldowns") in seen


@pytest.mark.asyncio
async def test_the_thiefs_own_bypass_cooldowns_toggle_is_honoured(monkeypatch):
    rt, msg = _rt(cooldown=False), _reply_msg()
    monkeypatch.setattr(
        dh, "debug_is_on", lambda uid, name: uid == THIEF and name == "bypass_cooldowns")
    assert await dh.attempt_steal(rt, msg, _challenge()) is True
    rt.duckhunt.handle_steal.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, age, judged", [
    ("recipe", 75, True),     # recipe gets 90s
    ("trivia", 75, False),    # trivia and captcha get 60s
    ("captcha", 75, False),
])
async def test_the_window_is_the_challenges_own_clock(kind, age, judged):
    rt, msg = _rt(), _reply_msg()
    assert await dh.attempt_steal(rt, msg, _challenge(kind, age=age)) is judged
    if not judged:                      # turned away before any lookup is paid for
        rt.db.fetchrow.assert_not_awaited()
        rt.duckhunt.stealable_duck.assert_not_awaited()
        rt.duckhunt.cooldown_ok.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_live_challenge_of_their_own_comes_first_and_costs_no_model_call():
    rt = _rt(own=_challenge("trivia", user_id=THIEF))
    msg = _reply_msg("g")
    assert await dh.attempt_steal(rt, msg, _challenge("trivia")) is True
    assert "own challenge" in msg.reply.await_args.args[0]
    rt.openai.cheap_chat.assert_not_awaited()
    rt.duckhunt.handle_steal.assert_not_awaited()


@pytest.mark.asyncio
async def test_their_own_challenge_that_has_run_out_is_no_gate():
    rt = _rt(own=_challenge("captcha", user_id=THIEF, age=300))
    msg = _reply_msg()
    assert await dh.attempt_steal(rt, msg, _challenge()) is True
    rt.duckhunt.clear_bef_challenge.assert_awaited_once_with(CHAT, THIEF)
    rt.duckhunt.handle_steal.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_thief_on_cooldown_does_not_burn_a_try_or_a_model_call():
    rt = _rt(cooldown=False, cheap="FAIL: no")
    ch = _challenge("trivia")
    for _ in range(dh.STEAL_TRIES + 2):
        await dh.attempt_steal(rt, _reply_msg("g"), ch)
    rt.openai.cheap_chat.assert_not_awaited()
    rt.duckhunt.cooldown_ok.return_value = True
    await dh.attempt_steal(rt, _reply_msg("g"), ch)
    assert rt.openai.cheap_chat.await_count == 1


@pytest.mark.asyncio
async def test_a_challenge_already_closed_is_not_judged():
    rt = _rt(current=None)
    msg = _reply_msg("the platypus")
    assert await dh.attempt_steal(rt, msg, _challenge("trivia")) is True
    rt.openai.cheap_chat.assert_not_awaited()
    assert "already closed" in msg.reply.await_args.args[0]
    assert not dh._steal_tries.get((CHAT, PROMPT, THIEF))        # no try spent


@pytest.mark.asyncio
async def test_a_challenge_replaced_by_a_newer_one_is_not_judged():
    rt = _rt(current=_challenge("trivia", prompt=PROMPT + 1))     # the owner got a new one
    msg = _reply_msg("the platypus")
    assert await dh.attempt_steal(rt, msg, _challenge("trivia")) is True
    rt.openai.cheap_chat.assert_not_awaited()
    assert "already closed" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status, said", [
    ("too_late", "already closed"), ("no_duck", "already gone"),
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


def test_one_lock_per_challenge_and_none_kept_for_an_idle_one():
    a = dh.challenge_lock(CHAT, OWNER)
    assert dh.challenge_lock(CHAT, OWNER) is a
    assert dh.challenge_lock(CHAT, OWNER + 1) is not a
    assert dh.challenge_lock(CHAT + 1, OWNER) is not a
    del a
    import gc
    gc.collect()
    assert (CHAT, OWNER) not in dh._challenge_locks


# ── the race, end to end: real service, in-memory statements ─────────────────

class _MemDB:
    """Just enough of the tables for the real DuckhuntService to run on."""

    def __init__(self):
        self.duck = {
            "id": DUCK, "chat_id": CHAT, "rarity": "common",
            "spawned_at": NOW - timedelta(minutes=10),
            "expires_at": NOW + timedelta(hours=1),
            "boss_required_hits": None, "boss_current_hits": 0,
            "resolved": False, "resolved_by": None,
        }
        self.challenge = {
            "chat_id": CHAT, "user_id": OWNER, "challenge": "Which mammal lays eggs?",
            "kind": "trivia", "prompt_message_id": PROMPT,
            "created_at": datetime.now(timezone.utc), "duck_id": DUCK,
        }
        self.stats = []

    async def fetchrow(self, sql, *args):
        if "FROM duck_events" in sql:
            return None if self.duck["resolved"] else dict(self.duck)
        if "FROM bef_challenges" in sql:
            c = self.challenge
            if c is None:
                return None
            if "prompt_message_id = $2" in sql:
                return dict(c) if args[1] == c["prompt_message_id"] else None
            return dict(c) if args[1] == c["user_id"] else None
        return None

    async def fetchval(self, sql, *args):
        return 0

    async def fetch(self, sql, *args):
        return []

    async def execute(self, sql, *args):
        await asyncio.sleep(0)
        if "UPDATE duck_events SET resolved = TRUE, resolved_by" in sql:
            if self.duck["resolved"]:
                return "UPDATE 0"
            self.duck["resolved"], self.duck["resolved_by"] = True, args[0]
            return "UPDATE 1"
        if "DELETE FROM bef_challenges" in sql:
            c = self.challenge
            if c and c["user_id"] == args[1] and c["prompt_message_id"] == args[2]:
                self.challenge = None
                return "DELETE 1"
            return "DELETE 0"
        if "INSERT INTO duck_stats" in sql:
            self.stats.append(args)
        return "OK"


def _race_rt(db, latencies):
    """The real service and the real chat handler. The judge is a script:
    `latencies[answer]` is how long that answer's model call takes."""
    rt = _rt_with(find_by_prompt=None, get_by_user=None)
    rt.settings.duckhunt_action_cooldown_seconds = 15
    rt.duckhunt = DuckhuntService(db)
    rt.db = db

    async def cheap_chat(messages, **kw):
        content = messages[0]["content"]
        for answer, delay in latencies.items():
            if f"---\n{answer}\n---" in content:
                await asyncio.sleep(delay)
                return "PASS: yes"
        return "FAIL: no"

    rt.openai = SimpleNamespace(cheap_chat=cheap_chat)
    return rt


def _mk(text, user_id):
    m = _msg(text=text, user_id=user_id, reply_to=SimpleNamespace(message_id=PROMPT))
    m.from_user.first_name = f"U{user_id}"
    m.reply = AsyncMock(return_value=SimpleNamespace(message_id=1))
    m.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    return m


@pytest.mark.asyncio
async def test_the_owner_who_answered_first_wins_even_if_judged_slower():
    db = _MemDB()
    rt = _race_rt(db, {"platypus": 0.30, "the platypus": 0.01})
    on_message = _find_message_handler(build_router(rt), "on_message")
    owner, thief = _mk("platypus", OWNER), _mk("the platypus", THIEF)

    async def thief_a_moment_later():
        await asyncio.sleep(0.05)
        await on_message(thief)

    await asyncio.gather(on_message(owner), thief_a_moment_later())
    assert db.duck["resolved"] is False                       # nobody took the duck
    assert db.challenge is None                               # the owner cleared it
    assert "Too late" not in owner.reply.await_args.args[0]    # they passed: the judge's "yes"
    assert "already closed" in thief.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_the_thief_who_answered_first_wins_even_if_judged_slower():
    db = _MemDB()
    rt = _race_rt(db, {"the platypus": 0.30, "platypus": 0.01})
    on_message = _find_message_handler(build_router(rt), "on_message")
    thief, owner = _mk("the platypus", THIEF), _mk("platypus", OWNER)

    async def owner_a_moment_later():
        await asyncio.sleep(0.05)
        await on_message(owner)

    await asyncio.gather(on_message(thief), owner_a_moment_later())
    assert db.duck["resolved"] is True and db.duck["resolved_by"] == THIEF
    assert db.challenge is None
    assert "duck is" in thief.reply.await_args.args[0]
    assert "Too late" in owner.reply.await_args.args[0]       # told, not "you passed"


@pytest.mark.asyncio
async def test_two_thieves_one_duck():
    db = _MemDB()
    rt = _race_rt(db, {"the platypus": 0.2, "a platypus": 0.01})
    on_message = _find_message_handler(build_router(rt), "on_message")
    first, second = _mk("the platypus", THIEF), _mk("a platypus", THIEF + 1)

    async def later():
        await asyncio.sleep(0.05)
        await on_message(second)

    await asyncio.gather(on_message(first), later())
    assert db.duck["resolved_by"] == THIEF
    assert len(db.stats) == 1 and db.stats[0][1] == THIEF     # paid once, to the winner
    assert "already closed" in second.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_a_wrong_answer_first_does_not_cost_the_right_one_the_win():
    db = _MemDB()
    rt = _race_rt(db, {"the platypus": 0.01})
    on_message = _find_message_handler(build_router(rt), "on_message")
    wrong, right = _mk("a kangaroo", THIEF), _mk("the platypus", THIEF + 1)
    await on_message(wrong)
    await on_message(right)
    assert db.duck["resolved_by"] == THIEF + 1
    assert wrong.reply.await_args.args[0] == "Not quite."


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

    async def get_bef_challenge(chat_id, user_id):
        return by_user if user_id == THIEF else (by_prompt or by_user)

    rt.duckhunt.get_bef_challenge = AsyncMock(side_effect=get_bef_challenge)
    return rt


@pytest.mark.asyncio
async def test_replying_to_someone_elses_prompt_with_the_answer_takes_the_duck():
    rt = _chat_rt(by_prompt=_challenge())
    on_message = _find_message_handler(build_router(rt), "on_message")
    msg = _reply_msg("RMY4X")
    await on_message(msg)
    rt.duckhunt.handle_steal.assert_awaited_once()
    rt.duckhunt.claim_bef_challenge.assert_not_called()      # that is handle_steal's
    msg.reply.assert_awaited()


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
async def test_free_text_is_never_a_steal_even_if_the_lookup_returns_another_players():
    """Defense in depth: the per-user lookup can only return the sender's own
    row. If it ever returned someone else's, typing is still not a reply to
    their prompt."""
    rt = _chat_rt(by_prompt=None, by_user=_challenge(user_id=OWNER))
    on_message = _find_message_handler(build_router(rt), "on_message")
    msg = _msg(text="RMY4X", user_id=THIEF, reply_to=None)
    try:
        await on_message(msg)
    except Exception:
        pass
    rt.duckhunt.stealable_duck.assert_not_awaited()
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
    try:
        await on_message(_reply_msg("RMY4X"))
    except Exception:
        pass
    rt.duckhunt.handle_steal.assert_not_awaited()
    rt.bot.me.assert_awaited()                         # went on past the intercept


@pytest.mark.asyncio
async def test_a_reply_after_the_owners_clock_is_handled_as_normal_chat():
    rt = _chat_rt(by_prompt=_challenge(age=120))       # past 60s, inside the 1h TTL
    on_message = _find_message_handler(build_router(rt), "on_message")
    try:
        await on_message(_reply_msg("RMY4X"))
    except Exception:
        pass
    rt.duckhunt.handle_steal.assert_not_awaited()
    rt.bot.me.assert_awaited()


@pytest.mark.asyncio
async def test_the_owner_replying_to_their_own_prompt_is_still_just_their_answer():
    rt = _chat_rt(by_prompt=_challenge(user_id=THIEF))
    on_message = _find_message_handler(build_router(rt), "on_message")
    msg = _reply_msg("RMY4X")
    await on_message(msg)
    rt.duckhunt.handle_steal.assert_not_awaited()
    rt.duckhunt.claim_bef_challenge.assert_awaited_once_with(CHAT, THIEF, PROMPT)


@pytest.mark.asyncio
async def test_an_owner_whose_challenge_was_taken_is_told_so_not_that_they_passed():
    rt = _chat_rt(by_prompt=_challenge(user_id=THIEF))
    rt.duckhunt.claim_bef_challenge = AsyncMock(return_value=False)
    on_message = _find_message_handler(build_router(rt), "on_message")
    msg = _reply_msg("RMY4X")
    await on_message(msg)
    assert "Too late" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_a_wrong_steal_is_replied_to_and_does_not_fall_into_chat():
    rt = _chat_rt(by_prompt=_challenge())
    on_message = _find_message_handler(build_router(rt), "on_message")
    msg = _reply_msg("WRONG")
    await on_message(msg)
    assert msg.reply.await_args.args[0] == "Not quite."
    rt.duckhunt.handle_steal.assert_not_awaited()


# ── issuing a challenge records the duck it is over ──────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["captcha", "trivia"])
async def test_a_challenge_is_issued_over_the_duck_it_was_asked_about(kind):
    rt = SimpleNamespace(
        duckhunt=SimpleNamespace(set_bef_challenge=AsyncMock()),
        openai=SimpleNamespace(cheap_completion=AsyncMock(return_value="Q?")),
    )
    msg = _msg(text="bef")
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=77))
    msg.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=77))
    assert await dh._issue_bef_challenge(
        rt, msg, "Luke", from_action="bef", force_kind=kind, duck_id=DUCK,
    ) is True
    assert rt.duckhunt.set_bef_challenge.await_args.kwargs["duck_id"] == DUCK
    await dh._issue_bef_challenge(rt, msg, "Luke", from_action="bef", force_kind=kind)
    assert rt.duckhunt.set_bef_challenge.await_args.kwargs["duck_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["refused", "cooldown"])
async def test_a_refused_or_rushed_bef_issues_its_challenge_over_that_duck(monkeypatch, path):
    from ipedro.duckhunt.scoring import ActionOutcome as AO

    issued = []

    async def fake_issue(rt_, msg_, who, *, from_action="bef", force_kind=None, duck_id=None):
        issued.append(duck_id)
        return True

    monkeypatch.setattr(dh, "_issue_bef_challenge", fake_issue)
    refusal = AO(success=False, points_delta=0, streak_delta=0,
                 message="no", resolves_duck=False)
    cfg = SimpleNamespace(duckhunt_enabled=True)
    rt = SimpleNamespace(
        chats=SimpleNamespace(
            upsert_chat=AsyncMock(), get_config=AsyncMock(return_value=cfg),
            upsert_default_config=AsyncMock(return_value=cfg)),
        users=SimpleNamespace(upsert_user=AsyncMock()),
        settings=SimpleNamespace(admin_ids=frozenset(),
                                 duckhunt_action_cooldown_seconds=15),
        db=SimpleNamespace(),
        duckhunt=SimpleNamespace(
            get_bef_challenge=AsyncMock(return_value=None),
            active_duck=AsyncMock(return_value=_duck(id=31)),
            cooldown_ok=AsyncMock(return_value=(path == "refused")),
            friend_count=AsyncMock(return_value=0),
            handle_bef=AsyncMock(return_value=(refusal, _duck(id=31))),
        ),
        openai=SimpleNamespace(cheap_chat=AsyncMock(return_value="REFUSE: no")),
        bot=SimpleNamespace(),
    )
    handler = next(h.callback for h in dh.build_router(rt).observers["message"].handlers
                   if h.callback.__name__ == "bef_action")
    msg = _msg(text="bef", user_id=THIEF)
    msg.reply = AsyncMock(return_value=SimpleNamespace(message_id=1))
    await handler(msg)
    assert issued == [31]
