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


class AlwaysPassRng:
    def random(self_inner):
        return 0.0  # always under any threshold


class AlwaysFailRng:
    def random(self_inner):
        return 0.999  # always over any threshold


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
        rng=AlwaysFailRng(),
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
        rng=AlwaysPassRng(),
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
        rng=AlwaysPassRng(),
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
        rng=AlwaysPassRng(),
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
        rng=AlwaysPassRng(),
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
    assert "no duck" in body.lower()
