"""Service-level test of the new bef flow.

The DB is faked with a thin in-memory stand-in. We only need:
  - active_duck() -> a single rarity-rolled duck
  - the side effects of _bump_stats / _resolve (we just record calls)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from ipedro.duckhunt.service import ActiveDuck, DuckhuntService


@dataclass
class _Calls:
    bumped: list[tuple] = None
    resolved: list[tuple] = None

    def __post_init__(self):
        self.bumped = []
        self.resolved = []


class FakeDB:
    """The minimum surface DuckhuntService.handle_bef needs."""

    def __init__(self, active: ActiveDuck | None):
        self._active = active
        self.calls = _Calls()

    async def fetchrow(self, query, *args):
        if "FROM duck_events" in query and "resolved = FALSE" in query:
            if not self._active:
                return None
            return {
                "id": self._active.id,
                "chat_id": self._active.chat_id,
                "rarity": self._active.rarity,
                "spawned_at": self._active.spawned_at,
                "expires_at": self._active.expires_at,
            }
        return None

    async def fetchval(self, query, *args):
        return 0  # streak

    async def execute(self, query, *args):
        if "INSERT INTO duck_stats" in query:
            self.calls.bumped.append(args)
        elif "UPDATE duck_events SET resolved = TRUE" in query:
            self.calls.resolved.append(args)
        return "UPDATE 1"

    async def fetch(self, query, *args):
        return []


def _duck(rarity: str) -> ActiveDuck:
    now = datetime.now(timezone.utc)
    return ActiveDuck(
        id=1, chat_id=42, rarity=rarity,
        spawned_at=now, expires_at=now + timedelta(hours=24),
    )


class AlwaysLowRng:
    """random() == 0.0 — always under any threshold. Makes bang_outcome
    hit and bef_dice_passes refuse (random() < BEF_REFUSE_RATE)."""
    def random(self_inner):
        return 0.0


class AlwaysHighRng:
    """random() == 0.999 — always over any threshold. Makes bef_dice_passes
    pass (random() >= BEF_REFUSE_RATE) and bang_outcome miss."""
    def random(self_inner):
        return 0.999


@pytest.mark.asyncio
async def test_ai_accept_resolves_regardless_of_rng():
    # Rarity is currently neutralized → the pre-AI dice gate always
    # passes, so an AI ACCEPT succeeds even with an rng that would have
    # failed the old rarity-biased dice. Replaces the previous
    # "dice_fail_short_circuits_ai" test (that gating no longer exists).
    db = FakeDB(_duck("legendary"))
    svc = DuckhuntService(db)  # type: ignore[arg-type]

    outcome, duck = await svc.handle_bef(
        chat_id=42, user_id=1, display_name="alice",
        ai_verdict=True,
        ai_line="i love you",
        rng=AlwaysHighRng(),
    )
    assert outcome is not None
    assert outcome.success is True
    assert outcome.resolves_duck is True
    assert "love you" in outcome.message
    assert len(db.calls.bumped) == 1
    assert len(db.calls.resolved) == 1


@pytest.mark.asyncio
async def test_dice_pass_and_ai_accept_resolves_and_awards():
    db = FakeDB(_duck("common"))
    svc = DuckhuntService(db)  # type: ignore[arg-type]

    outcome, _ = await svc.handle_bef(
        chat_id=42, user_id=1, display_name="alice",
        ai_verdict=True, ai_line="the duck nods",
        rng=AlwaysHighRng(),
    )
    assert outcome is not None
    assert outcome.success is True
    assert outcome.resolves_duck is True
    assert "nods" in outcome.message
    assert len(db.calls.bumped) == 1
    assert len(db.calls.resolved) == 1


@pytest.mark.asyncio
async def test_dice_pass_but_ai_refuse_keeps_duck():
    db = FakeDB(_duck("rare"))
    svc = DuckhuntService(db)  # type: ignore[arg-type]

    outcome, _ = await svc.handle_bef(
        chat_id=42, user_id=1, display_name="alice",
        ai_verdict=False, ai_line="duck is too cool for you",
        rng=AlwaysHighRng(),
    )
    assert outcome is not None
    assert outcome.success is False
    assert outcome.resolves_duck is False
    assert "too cool" in outcome.message
    assert db.calls.bumped == []
    assert db.calls.resolved == []


@pytest.mark.asyncio
async def test_ai_unavailable_falls_back_to_dice_accept():
    db = FakeDB(_duck("common"))
    svc = DuckhuntService(db)  # type: ignore[arg-type]

    outcome, _ = await svc.handle_bef(
        chat_id=42, user_id=1, display_name="alice",
        ai_verdict=None,  # AI unavailable
        ai_line=None,
        rng=AlwaysHighRng(),
    )
    assert outcome is not None
    assert outcome.success is True
    assert outcome.resolves_duck is True
    assert len(db.calls.bumped) == 1
    assert len(db.calls.resolved) == 1


@pytest.mark.asyncio
async def test_no_duck_returns_none_pair():
    db = FakeDB(active=None)
    svc = DuckhuntService(db)  # type: ignore[arg-type]

    outcome, duck = await svc.handle_bef(
        chat_id=42, user_id=1, display_name="alice",
        ai_verdict=True, ai_line="hi",
        rng=AlwaysHighRng(),
    )
    assert outcome is None and duck is None


# --- handler-level regression: typing `bang` with no active duck should
# reply, not silently swallow the message. ------------------------------
@pytest.mark.asyncio
async def test_bang_without_duck_replies():
    """Regression test for the silent-no-duck bug (Fix 2).

    Before this fix the handler returned early without replying when
    ``handle_bang`` returned ``(None, None)``. Users typing `bang` got
    no feedback. The fix replies with a brief "🦆 No duck here." line.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ipedro.handlers.duckhunt import build_router

    cfg = SimpleNamespace(duckhunt_enabled=True)
    chats = SimpleNamespace(
        upsert_chat=AsyncMock(),
        get_config=AsyncMock(return_value=cfg),
        upsert_default_config=AsyncMock(return_value=cfg),
    )
    users = SimpleNamespace(upsert_user=AsyncMock())
    duckhunt = SimpleNamespace(
        active_duck=AsyncMock(return_value=None),
        cooldown_ok=AsyncMock(return_value=True),
        # No pending bef/bang-miss challenge for this user.
        get_bef_challenge=AsyncMock(return_value=None),
        # No active duck → handle_bang returns the (None, None) sentinel.
        handle_bang=AsyncMock(return_value=(None, None)),
        handle_ignore=AsyncMock(return_value=(None, None)),
    )
    settings = SimpleNamespace(
        admin_ids=frozenset(),
        duckhunt_action_cooldown_seconds=15,
        duckhunt_duck_lifetime_seconds=86400,
    )
    rt = SimpleNamespace(
        settings=settings, db=SimpleNamespace(), chats=chats, users=users,
        duckhunt=duckhunt, openai=SimpleNamespace(), bot=SimpleNamespace(),
    )

    router = build_router(rt)
    # Pull the bang_or_ignore handler out of the router so we can call
    # it directly.
    handler = None
    for h in router.observers["message"].handlers:
        if h.callback.__name__ == "bang_or_ignore":
            handler = h.callback
            break
    assert handler is not None

    chat = SimpleNamespace(id=42, type="group", title="t")
    from_user = SimpleNamespace(
        id=7, is_bot=False, username="u",
        first_name="U", last_name=None,
    )
    msg = SimpleNamespace(
        chat=chat, from_user=from_user, text="bang", caption=None,
        message_id=1, reply=AsyncMock(),
    )
    await handler(msg)

    # handle_bang returned (None, None) → the no-duck reply branch fired.
    duckhunt.handle_bang.assert_awaited_once()
    msg.reply.assert_awaited()
    body = msg.reply.await_args.args[0]
    # The no-duck reply is now picked from a randomized flame pool, so
    # the assertion can't pin a literal phrase. It's enough to confirm
    # the reply came from that pool (not "Cool it. Cooldown." etc.).
    from ipedro.handlers.duckhunt import _NO_DUCK_FLAVOR
    assert body in _NO_DUCK_FLAVOR


# Bang-miss → challenge path -----------------------------------------------
@pytest.mark.asyncio
async def test_bang_miss_triggers_challenge_when_dice_says_so(monkeypatch):
    """After a missed bang, ``should_challenge_on_miss`` decides whether to
    issue a captcha/trivia/recipe. When it says yes, ``_issue_bef_challenge``
    fires with a 'spooked' intro."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ipedro.duckhunt.scoring import ActionOutcome
    from ipedro.handlers import duckhunt as dh

    cfg = SimpleNamespace(duckhunt_enabled=True)
    chats = SimpleNamespace(
        upsert_chat=AsyncMock(),
        get_config=AsyncMock(return_value=cfg),
        upsert_default_config=AsyncMock(return_value=cfg),
    )
    users = SimpleNamespace(upsert_user=AsyncMock())
    miss = ActionOutcome(
        success=False, points_delta=0, streak_delta=0,
        message="You missed!", resolves_duck=False,
    )
    duck = _duck("common")
    duckhunt = SimpleNamespace(
        active_duck=AsyncMock(return_value=duck),
        cooldown_ok=AsyncMock(return_value=True),
        get_bef_challenge=AsyncMock(return_value=None),
        handle_bang=AsyncMock(return_value=(miss, duck)),
        handle_ignore=AsyncMock(return_value=(None, None)),
    )
    settings = SimpleNamespace(
        admin_ids=frozenset(),
        duckhunt_action_cooldown_seconds=15,
        duckhunt_duck_lifetime_seconds=86400,
    )
    rt = SimpleNamespace(
        settings=settings, db=SimpleNamespace(), chats=chats, users=users,
        duckhunt=duckhunt, openai=SimpleNamespace(), bot=SimpleNamespace(),
    )

    # Force the miss-challenge dice to fire.
    monkeypatch.setattr(dh, "should_challenge_on_miss", lambda *a, **k: True)

    issued: list[dict] = []
    async def fake_issue(rt_, msg_, who, *, from_action="bef", force_kind=None):
        issued.append({"from_action": from_action, "who": who})
        return True
    monkeypatch.setattr(dh, "_issue_bef_challenge", fake_issue)

    router = dh.build_router(rt)
    handler = next(
        h.callback for h in router.observers["message"].handlers
        if h.callback.__name__ == "bang_or_ignore"
    )

    chat = SimpleNamespace(id=42, type="group", title="t")
    from_user = SimpleNamespace(
        id=7, is_bot=False, username="u", first_name="U", last_name=None,
    )
    msg = SimpleNamespace(
        chat=chat, from_user=from_user, text="bang", caption=None,
        message_id=1, reply=AsyncMock(), answer=AsyncMock(),
    )
    await handler(msg)

    duckhunt.handle_bang.assert_awaited_once()
    msg.reply.assert_awaited()
    assert len(issued) == 1
    # The bang-miss path now sets from_action="bang_miss" instead of
    # passing a literal intro string; the helper picks flavor from the
    # per-action pool.
    assert issued[0]["from_action"] == "bang_miss"


@pytest.mark.asyncio
async def test_bang_blocked_while_challenge_pending():
    """A user with an outstanding challenge can't bang — they get nudged
    to solve the challenge first, and handle_bang is never called."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ipedro.handlers.duckhunt import build_router

    cfg = SimpleNamespace(duckhunt_enabled=True)
    chats = SimpleNamespace(
        upsert_chat=AsyncMock(),
        get_config=AsyncMock(return_value=cfg),
        upsert_default_config=AsyncMock(return_value=cfg),
    )
    users = SimpleNamespace(upsert_user=AsyncMock())
    duckhunt = SimpleNamespace(
        active_duck=AsyncMock(),
        cooldown_ok=AsyncMock(return_value=True),
        # A pending challenge (any truthy sentinel).
        get_bef_challenge=AsyncMock(return_value=SimpleNamespace(kind="captcha")),
        handle_bang=AsyncMock(),
        handle_ignore=AsyncMock(),
    )
    settings = SimpleNamespace(
        admin_ids=frozenset(),
        duckhunt_action_cooldown_seconds=15,
        duckhunt_duck_lifetime_seconds=86400,
    )
    rt = SimpleNamespace(
        settings=settings, db=SimpleNamespace(), chats=chats, users=users,
        duckhunt=duckhunt, openai=SimpleNamespace(), bot=SimpleNamespace(),
    )

    router = build_router(rt)
    handler = next(
        h.callback for h in router.observers["message"].handlers
        if h.callback.__name__ == "bang_or_ignore"
    )

    chat = SimpleNamespace(id=42, type="group", title="t")
    from_user = SimpleNamespace(
        id=7, is_bot=False, username="u", first_name="U", last_name=None,
    )
    msg = SimpleNamespace(
        chat=chat, from_user=from_user, text="bang", caption=None,
        message_id=1, reply=AsyncMock(),
    )
    await handler(msg)

    duckhunt.handle_bang.assert_not_called()
    msg.reply.assert_awaited()
    body = msg.reply.await_args.args[0].lower()
    assert "challenge" in body


def test_random_challenge_kinds_are_biased_toward_captcha():
    """The random-kind picker uses weighted choice; over many draws the
    captcha share should sit close to the configured weight ratio."""
    import random
    from ipedro.handlers.duckhunt import (
        _RANDOM_CHALLENGE_KINDS, _RANDOM_CHALLENGE_WEIGHTS,
    )

    rng = random.Random(0)
    n = 4000
    counts = {"captcha": 0, "trivia": 0}
    for _ in range(n):
        kind = rng.choices(_RANDOM_CHALLENGE_KINDS,
                           weights=_RANDOM_CHALLENGE_WEIGHTS)[0]
        counts[kind] += 1

    captcha_share = counts["captcha"] / n
    expected = (_RANDOM_CHALLENGE_WEIGHTS[0]
                / sum(_RANDOM_CHALLENGE_WEIGHTS))
    assert abs(captcha_share - expected) < 0.03
    # Sanity: bias really did shift the balance away from 50/50.
    assert captcha_share > 0.6


# --- spawner cow-guard ---------------------------------------------------
def test_looks_like_a_duck_rejects_cow_ascii():
    from ipedro.duckhunt.spawner import _looks_like_a_duck

    cow = "  ^__^\n  (oo)\\_______\n  (__)\\       )\\/\\\n      ||----w |\n      ||     ||\n\n  QUACK! 🦆"
    duck = "  __\n<('< 🦆 quack!"
    nope = "hello world"   # no quack at all → rejected
    assert _looks_like_a_duck(cow) is False
    assert _looks_like_a_duck(duck) is True
    assert _looks_like_a_duck(nope) is False


# --- reply-to-name handler ------------------------------------------------
@pytest.mark.asyncio
async def test_reply_to_celebration_sets_duck_name():
    """A reply to a 'Want to name them?' celebration sets the duck's
    name without the user having to type /duckname N <name>."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ipedro.handlers import duckhunt as dh

    cfg = SimpleNamespace(duckhunt_enabled=True)
    chats = SimpleNamespace(
        upsert_chat=AsyncMock(),
        get_config=AsyncMock(return_value=cfg),
        upsert_default_config=AsyncMock(return_value=cfg),
    )
    users = SimpleNamespace(upsert_user=AsyncMock())
    duckhunt = SimpleNamespace(
        name_duck=AsyncMock(return_value=True),
    )
    settings = SimpleNamespace(
        admin_ids=frozenset(),
        duckhunt_action_cooldown_seconds=15,
        duckhunt_duck_lifetime_seconds=86400,
    )
    rt = SimpleNamespace(
        settings=settings, db=SimpleNamespace(), chats=chats, users=users,
        duckhunt=duckhunt, openai=SimpleNamespace(), bot=SimpleNamespace(),
    )

    # Pretend a celebration message was sent for duck #63 by user 7.
    dh._PENDING_NAMING.clear()
    dh._register_pending_name(
        chat_id=42, prompt_msg_id=100, user_id=7, duck_id=63,
    )

    router = dh.build_router(rt)
    handler = next(
        h.callback for h in router.observers["message"].handlers
        if h.callback.__name__ == "name_on_reply"
    )

    chat = SimpleNamespace(id=42, type="group", title="t")
    from_user = SimpleNamespace(
        id=7, is_bot=False, username="u", first_name="U", last_name=None,
    )
    reply_to = SimpleNamespace(message_id=100)
    msg = SimpleNamespace(
        chat=chat, from_user=from_user, text="Grace's Salty Tears",
        caption=None, message_id=200, reply_to_message=reply_to,
        reply=AsyncMock(),
    )
    await handler(msg)

    duckhunt.name_duck.assert_awaited_once_with(
        42, 7, 63, "Grace's Salty Tears",
    )
    body = msg.reply.await_args.args[0]
    assert "63" in body and "Grace" in body
    # Pending state consumed after a successful name.
    assert (42, 100) not in dh._PENDING_NAMING


@pytest.mark.asyncio
async def test_reply_to_naming_filter_rejects_other_user():
    """Only the user who befriended the duck can name it via reply —
    another user's reply to the same prompt is not a match."""
    from types import SimpleNamespace

    from ipedro.handlers import duckhunt as dh

    dh._PENDING_NAMING.clear()
    dh._register_pending_name(
        chat_id=42, prompt_msg_id=100, user_id=7, duck_id=63,
    )

    msg = SimpleNamespace(
        chat=SimpleNamespace(id=42),
        from_user=SimpleNamespace(id=999),       # different user
        reply_to_message=SimpleNamespace(message_id=100),
        text="nice try",
        caption=None,
    )
    assert await dh._is_naming_reply(msg) is False
