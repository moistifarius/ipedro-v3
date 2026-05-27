"""Duckhunt persistence + lifecycle (DB-backed)."""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ipedro.db.pool import Database
from ipedro.duckhunt.scoring import (
    ActionOutcome, bang_outcome, bef_dice_passes, bef_refusal_outcome,
    bef_success_outcome, ignore_outcome, roll_rarity,
)

log = logging.getLogger(__name__)


@dataclass
class ActiveDuck:
    id: int
    chat_id: int
    rarity: str
    spawned_at: datetime
    expires_at: datetime


@dataclass
class PendingBefChallenge:
    chat_id: int
    user_id: int
    challenge: str
    kind: str
    prompt_message_id: int | None
    created_at: datetime


def _row_to_active_duck(row) -> ActiveDuck:
    return ActiveDuck(
        id=row["id"], chat_id=row["chat_id"], rarity=row["rarity"],
        spawned_at=row["spawned_at"], expires_at=row["expires_at"],
    )


class DuckhuntService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------ spawn
    async def active_duck(self, chat_id: int) -> ActiveDuck | None:
        row = await self.db.fetchrow(
            "SELECT id, chat_id, rarity, spawned_at, expires_at "
            "  FROM duck_events "
            " WHERE chat_id = $1 AND resolved = FALSE "
            "   AND expires_at > NOW() "
            " ORDER BY id DESC LIMIT 1",
            chat_id,
        )
        return _row_to_active_duck(row) if row else None

    async def spawn_duck(
        self, chat_id: int, lifetime_seconds: int,
        rng: random.Random | None = None,
    ) -> ActiveDuck:
        # Resolve any stale unexpired duck first (idempotency).
        await self.expire_old_ducks(chat_id)
        rarity = roll_rarity(rng)
        expires = datetime.now(timezone.utc) + timedelta(seconds=lifetime_seconds)
        row = await self.db.fetchrow(
            "INSERT INTO duck_events (chat_id, rarity, expires_at) "
            "VALUES ($1, $2, $3) "
            "RETURNING id, chat_id, rarity, spawned_at, expires_at",
            chat_id, rarity, expires,
        )
        assert row is not None
        log.info("Spawned %s duck in chat %s (event id=%s).", rarity, chat_id, row["id"])
        return _row_to_active_duck(row)

    async def expire_old_ducks(self, chat_id: int | None = None) -> int:
        if chat_id is not None:
            res = await self.db.execute(
                "UPDATE duck_events SET resolved = TRUE, resolved_action = 'expired', "
                " resolved_at = NOW() "
                " WHERE chat_id = $1 AND resolved = FALSE AND expires_at <= NOW()",
                chat_id,
            )
        else:
            res = await self.db.execute(
                "UPDATE duck_events SET resolved = TRUE, resolved_action = 'expired', "
                " resolved_at = NOW() "
                " WHERE resolved = FALSE AND expires_at <= NOW()"
            )
        try:
            return int(res.split()[-1])
        except Exception:
            return 0

    async def probabilistically_depart(
        self,
        tick_seconds: float,
        half_life_seconds: float,
        rng: random.Random | None = None,
    ) -> list[int]:
        """For each active duck, roll the leave-die based on tick_seconds and
        the configured half-life. Returns the chat_ids that lost their duck so
        the spawner can announce departures if it likes.
        """
        r = rng if rng is not None else random
        rows = await self.db.fetch(
            "SELECT id, chat_id FROM duck_events "
            " WHERE resolved = FALSE AND expires_at > NOW()"
        )
        departed: list[int] = []
        # P(leave during tick) = 1 - 0.5^(tick / half_life)
        if half_life_seconds <= 0:
            return departed
        p_leave = 1.0 - math.pow(0.5, tick_seconds / half_life_seconds)
        for row in rows:
            if r.random() <= p_leave:
                await self.db.execute(
                    "UPDATE duck_events SET resolved = TRUE, "
                    " resolved_action = 'departed', resolved_at = NOW() "
                    " WHERE id = $1 AND resolved = FALSE",
                    row["id"],
                )
                departed.append(row["chat_id"])
        return departed

    # ------------------------------------------------------------ resolution helpers
    async def _resolve(
        self, event_id: int, user_id: int, action: str, points: int,
    ) -> None:
        await self.db.execute(
            "UPDATE duck_events SET resolved = TRUE, resolved_by = $1, "
            "resolved_action = $2, resolved_at = NOW(), points_awarded = $3 "
            "WHERE id = $4 AND resolved = FALSE",
            user_id, action, points, event_id,
        )

    async def _bump_stats(
        self,
        chat_id: int,
        user_id: int,
        display_name: str,
        action: str,
        outcome: ActionOutcome,
    ) -> None:
        killed = befriended = ignored = misses = 0
        if action == "bang":
            if outcome.success:
                killed = 1
            else:
                misses = 1
        elif action == "bef" and outcome.success:
            befriended = 1
        elif action == "ignore":
            # 'ignore' is never tallied as a real action stat increment here -
            # see also the "no-op on bef refusal" rule. Original V1 counted
            # ignore separately; we mirror that for back-compat.
            ignored = 1

        await self.db.execute(
            """
            INSERT INTO duck_stats (chat_id, user_id, display_name, killed, befriended,
                                    ignored, misses, points, streak,
                                    best_streak, last_action_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                    GREATEST($9, 0),
                    GREATEST($9, 0),
                    NOW())
            ON CONFLICT (chat_id, user_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                killed       = duck_stats.killed     + EXCLUDED.killed,
                befriended   = duck_stats.befriended + EXCLUDED.befriended,
                ignored      = duck_stats.ignored    + EXCLUDED.ignored,
                misses       = duck_stats.misses     + EXCLUDED.misses,
                points       = duck_stats.points     + EXCLUDED.points,
                streak       = GREATEST(0, duck_stats.streak + $9),
                best_streak  = GREATEST(duck_stats.best_streak,
                                         GREATEST(0, duck_stats.streak + $9)),
                last_action_at = NOW()
            """,
            chat_id, user_id, display_name,
            killed, befriended, ignored, misses,
            outcome.points_delta, outcome.streak_delta,
        )

    # ------------------------------------------------------------ public actions
    async def cooldown_ok(self, chat_id: int, user_id: int, cooldown_seconds: int) -> bool:
        row = await self.db.fetchrow(
            "SELECT last_action_at FROM duck_stats WHERE chat_id = $1 AND user_id = $2",
            chat_id, user_id,
        )
        if not row or not row["last_action_at"]:
            return True
        delta = (datetime.now(timezone.utc) - row["last_action_at"]).total_seconds()
        return delta >= cooldown_seconds

    async def handle_bang(
        self, *, chat_id: int, user_id: int, display_name: str,
        rng: random.Random | None = None,
    ) -> tuple[ActionOutcome, ActiveDuck] | tuple[None, None]:
        duck = await self.active_duck(chat_id)
        if not duck:
            return None, None
        stats = await self.db.fetchval(
            "SELECT COALESCE(streak, 0) FROM duck_stats "
            "WHERE chat_id = $1 AND user_id = $2",
            chat_id, user_id,
        ) or 0
        outcome = bang_outcome(duck.rarity, int(stats), rng)
        await self._bump_stats(chat_id, user_id, display_name, "bang", outcome)
        if outcome.resolves_duck:
            await self._resolve(duck.id, user_id, "bang", outcome.points_delta)
        return outcome, duck

    async def handle_ignore(
        self, *, chat_id: int, user_id: int, display_name: str,
        rng: random.Random | None = None,
    ) -> tuple[ActionOutcome, ActiveDuck] | tuple[None, None]:
        duck = await self.active_duck(chat_id)
        if not duck:
            return None, None
        outcome = ignore_outcome(duck.rarity, rng)
        await self._bump_stats(chat_id, user_id, display_name, "ignore", outcome)
        if outcome.resolves_duck:
            await self._resolve(duck.id, user_id, "ignore", outcome.points_delta)
        return outcome, duck

    async def handle_bef(
        self,
        *,
        chat_id: int,
        user_id: int,
        display_name: str,
        ai_verdict: bool | None,
        ai_line: str | None,
        rng: random.Random | None = None,
    ) -> tuple[ActionOutcome, ActiveDuck] | tuple[None, None]:
        """Apply the two-stage bef flow.

        Caller is expected to have:
          - confirmed the global cooldown is ok
          - confirmed there's no pending bef challenge for this user
          - already invoked the AI for `ai_verdict` (True=ACCEPT, False=REFUSE,
            None=AI unavailable). When None, we fall back to dice alone.
        """
        duck = await self.active_duck(chat_id)
        if not duck:
            return None, None

        dice_pass = bef_dice_passes(duck.rarity, rng)

        # Dice fail short-circuits the AI: refusal, duck stays, no stats.
        if not dice_pass:
            outcome = bef_refusal_outcome(ai_line if ai_verdict is False else None)
            # No stats bump on refusal per spec ("no op").
            return outcome, duck

        # Dice passed - consult AI verdict (which the caller obtained).
        if ai_verdict is None:
            # AI unavailable -> fall back to dice (which passed) = accept.
            outcome = bef_success_outcome(duck.rarity, None)
        elif ai_verdict is True:
            outcome = bef_success_outcome(duck.rarity, ai_line)
        else:
            outcome = bef_refusal_outcome(ai_line)

        if outcome.success:
            await self._bump_stats(chat_id, user_id, display_name, "bef", outcome)
            await self._resolve(duck.id, user_id, "bef", outcome.points_delta)
        # Refusal => no stat bump, duck stays.

        return outcome, duck

    # ------------------------------------------------------------ stats / roster
    async def leaderboard(self, chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT user_id, display_name, points, killed, befriended, ignored,
                   misses, streak, best_streak
              FROM duck_stats
             WHERE chat_id = $1
             ORDER BY points DESC, killed DESC
             LIMIT $2
            """,
            chat_id, limit,
        )
        return [dict(r) for r in rows]

    async def user_stats(self, chat_id: int, user_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            "SELECT * FROM duck_stats WHERE chat_id = $1 AND user_id = $2",
            chat_id, user_id,
        )
        return dict(row) if row else None

    async def friend_count(self, chat_id: int, user_id: int) -> int:
        val = await self.db.fetchval(
            "SELECT COUNT(*) FROM duck_events "
            " WHERE chat_id = $1 AND resolved_by = $2 AND resolved_action = 'bef'",
            chat_id, user_id,
        )
        return int(val or 0)

    async def friendship_roster(
        self, chat_id: int, user_id: int, limit: int = 20,
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT id, rarity, resolved_at
              FROM duck_events
             WHERE chat_id = $1 AND resolved_by = $2 AND resolved_action = 'bef'
             ORDER BY resolved_at DESC
             LIMIT $3
            """,
            chat_id, user_id, limit,
        )
        return [dict(r) for r in rows]

    async def quack_flag(self, chat_id: int) -> bool:
        return await self.active_duck(chat_id) is not None

    # ------------------------------------------------------------ bef challenges
    async def get_bef_challenge(
        self, chat_id: int, user_id: int,
    ) -> PendingBefChallenge | None:
        row = await self.db.fetchrow(
            "SELECT chat_id, user_id, challenge, kind, prompt_message_id, created_at "
            "  FROM bef_challenges WHERE chat_id = $1 AND user_id = $2",
            chat_id, user_id,
        )
        return PendingBefChallenge(**dict(row)) if row else None

    async def set_bef_challenge(
        self, chat_id: int, user_id: int, challenge: str, kind: str,
        prompt_message_id: int | None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO bef_challenges (chat_id, user_id, challenge, kind,
                                        prompt_message_id, created_at)
            VALUES ($1, $2, $3, $4, $5, NOW())
            ON CONFLICT (chat_id, user_id) DO UPDATE SET
                challenge = EXCLUDED.challenge,
                kind = EXCLUDED.kind,
                prompt_message_id = EXCLUDED.prompt_message_id,
                created_at = NOW()
            """,
            chat_id, user_id, challenge, kind, prompt_message_id,
        )

    async def clear_bef_challenge(self, chat_id: int, user_id: int) -> None:
        await self.db.execute(
            "DELETE FROM bef_challenges WHERE chat_id = $1 AND user_id = $2",
            chat_id, user_id,
        )

    async def find_bef_challenge_by_prompt(
        self, chat_id: int, prompt_message_id: int,
    ) -> PendingBefChallenge | None:
        row = await self.db.fetchrow(
            "SELECT chat_id, user_id, challenge, kind, prompt_message_id, created_at "
            "  FROM bef_challenges "
            " WHERE chat_id = $1 AND prompt_message_id = $2",
            chat_id, prompt_message_id,
        )
        return PendingBefChallenge(**dict(row)) if row else None
