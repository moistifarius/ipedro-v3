"""Boss-duck accounting and the bef/ignore gates.

Regressions covered:
  - a 3-hit boss used to add 3+ to `killed` across the chat (every hit and
    every participation share counted as a kill) — only the killing blow may.
  - `ignore` used to bypass the pending-challenge gate that blocks `bang`.
  - `bef` on cooldown used to hand out a captcha even with NO duck present.
  - `bef` on a boss used to swallow the explanation and serve a captcha.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.duckhunt.scoring import ActionOutcome
from ipedro.duckhunt.service import ActiveDuck, DuckhuntService


def _boss(chat_id=42, required=3, current=0) -> ActiveDuck:
    now = datetime.now(timezone.utc)
    return ActiveDuck(
        id=7, chat_id=chat_id, rarity="common",
        spawned_at=now, expires_at=now + timedelta(hours=24),
        boss_required_hits=required, boss_current_hits=current,
    )


class _BossFakeDB:
    """Just enough surface for _handle_bang_boss + handle_bef."""

    def __init__(self, duck: ActiveDuck, *, next_hits: int,
                 contributors: list[dict] | None = None):
        self._duck = duck
        self._next_hits = next_hits          # value RETURNING boss_current_hits
        self._contributors = contributors or []
        self.bumps: list[tuple] = []         # duck_stats INSERT args

    async def fetchrow(self, query, *args):
        if "FROM duck_events" in query and "resolved = FALSE" in query:
            d = self._duck
            return {
                "id": d.id, "chat_id": d.chat_id, "rarity": d.rarity,
                "spawned_at": d.spawned_at, "expires_at": d.expires_at,
                "boss_required_hits": d.boss_required_hits,
                "boss_current_hits": d.boss_current_hits,
            }
        return None

    async def fetchval(self, query, *args):
        if "RETURNING boss_current_hits" in query:
            return self._next_hits
        return 0

    async def fetch(self, query, *args):
        if "FROM duck_boss_hits" in query:
            return self._contributors
        return []

    async def execute(self, query, *args):
        if "INSERT INTO duck_stats" in query:
            self.bumps.append(args)
        return "UPDATE 1"


# duck_stats INSERT arg order:
# chat_id, user_id, display_name, killed, befriended, ignored, misses, points, streak
_KILLED = 3


@pytest.mark.asyncio
async def test_non_killing_boss_hit_is_not_a_kill():
    db = _BossFakeDB(_boss(required=3), next_hits=1)
    svc = DuckhuntService(db)  # type: ignore[arg-type]
    outcome, _ = await svc.handle_bang(
        chat_id=42, user_id=1, display_name="Matt",
    )
    assert outcome.success and not outcome.resolves_duck
    assert len(db.bumps) == 1
    assert db.bumps[0][_KILLED] == 0          # hit ≠ kill
    assert outcome.points_delta > 0           # but points still flow


@pytest.mark.asyncio
async def test_killing_blow_credits_exactly_one_kill():
    contributors = [
        {"user_id": 1, "display_name": "Matt", "hits": 2},
        {"user_id": 2, "display_name": "Luke", "hits": 1},
    ]
    db = _BossFakeDB(_boss(required=3), next_hits=3, contributors=contributors)
    svc = DuckhuntService(db)  # type: ignore[arg-type]
    outcome, _ = await svc.handle_bang(
        chat_id=42, user_id=1, display_name="Matt",
    )
    assert outcome.success and outcome.resolves_duck
    kills = [b for b in db.bumps if b[_KILLED] == 1]
    assert len(kills) == 1                    # ONE duck → ONE kill, total
    assert kills[0][1] == 1                   # ...credited to the killer
    # participation share for Luke carries points but no kill
    shares = [b for b in db.bumps if b[1] == 2]
    assert len(shares) == 1 and shares[0][_KILLED] == 0


@pytest.mark.asyncio
async def test_bef_on_boss_explains_instead_of_challenging():
    db = _BossFakeDB(_boss(), next_hits=0)
    svc = DuckhuntService(db)  # type: ignore[arg-type]
    outcome, duck = await svc.handle_bef(
        chat_id=42, user_id=1, display_name="Matt",
        ai_verdict=None, ai_line=None,
    )
    assert outcome is not None and not outcome.success
    assert "too big" in outcome.message
    assert "`" not in outcome.message         # parse_mode=None → no markdown
    assert db.bumps == []                     # refusal is a stat no-op


# ── handler-level gates ──────────────────────────────────────────────────────

def _handler(rt, name):
    from ipedro.handlers.duckhunt import build_router
    router = build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == name)


def _rt(*, pending=None, duck=None, cooldown_ok=True):
    cfg = SimpleNamespace(duckhunt_enabled=True, automod_enabled=True,
                          response_policy="always")
    return SimpleNamespace(
        settings=SimpleNamespace(
            admin_ids=frozenset(), duckhunt_action_cooldown_seconds=15,
        ),
        chats=SimpleNamespace(
            upsert_chat=AsyncMock(), get_config=AsyncMock(return_value=cfg),
            upsert_default_config=AsyncMock(return_value=cfg),
        ),
        users=SimpleNamespace(upsert_user=AsyncMock()),
        duckhunt=SimpleNamespace(
            get_bef_challenge=AsyncMock(return_value=pending),
            cooldown_ok=AsyncMock(return_value=cooldown_ok),
            active_duck=AsyncMock(return_value=duck),
            handle_bang=AsyncMock(return_value=(None, None)),
            handle_ignore=AsyncMock(return_value=(None, None)),
            handle_bef=AsyncMock(return_value=(None, None)),
            friend_count=AsyncMock(return_value=0),
        ),
        openai=SimpleNamespace(cheap_chat=AsyncMock(return_value="ACCEPT ok")),
        db=SimpleNamespace(),
        bot=SimpleNamespace(),
    )


def _msg(text):
    return SimpleNamespace(
        chat=SimpleNamespace(id=42, type="group", title="t"),
        from_user=SimpleNamespace(id=7, is_bot=False, username="u",
                                  first_name="U", last_name=None),
        text=text, message_id=100,
        reply=AsyncMock(return_value=SimpleNamespace(message_id=101)),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=102)),
    )


@pytest.mark.asyncio
async def test_ignore_is_blocked_by_pending_challenge():
    rt = _rt(pending=SimpleNamespace(kind="captcha"))
    handler = _handler(rt, "bang_or_ignore")
    msg = _msg("ignore")
    await handler(msg)
    rt.duckhunt.handle_ignore.assert_not_awaited()   # gate held
    assert "challenge" in msg.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_bef_with_no_duck_on_cooldown_gets_no_captcha():
    rt = _rt(duck=None, cooldown_ok=False)
    handler = _handler(rt, "bef_action")
    msg = _msg("bef")
    await handler(msg)
    # No duck → told so; the cooldown-challenge branch must NOT fire.
    rt.duckhunt.handle_bef.assert_not_awaited()
    text = msg.reply.await_args.args[0].lower()
    assert "challenge" not in text
    rt.duckhunt.get_bef_challenge.assert_awaited()   # gate still consulted
