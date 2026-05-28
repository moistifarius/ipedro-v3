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
