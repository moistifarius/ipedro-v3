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
    ActionOutcome, bang_outcome, base_points, bef_dice_passes,
    bef_refusal_outcome, bef_success_outcome, boss_required_hits,
    current_holiday, ignore_outcome, roll_is_boss, roll_rarity,
)

log = logging.getLogger(__name__)


@dataclass
class ActiveDuck:
    id: int
    chat_id: int
    rarity: str
    spawned_at: datetime
    expires_at: datetime
    boss_required_hits: int | None = None
    boss_current_hits: int = 0

    @property
    def is_boss(self) -> bool:
        return self.boss_required_hits is not None


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
        boss_required_hits=row.get("boss_required_hits") if isinstance(row, dict)
            else row["boss_required_hits"],
        boss_current_hits=(
            row.get("boss_current_hits", 0) if isinstance(row, dict)
            else (row["boss_current_hits"] or 0)
        ),
    )


class DuckhuntService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------ spawn
    async def active_duck(self, chat_id: int) -> ActiveDuck | None:
        row = await self.db.fetchrow(
            "SELECT id, chat_id, rarity, spawned_at, expires_at, "
            "       boss_required_hits, boss_current_hits "
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
        on_holiday = current_holiday() is not None
        rarity = roll_rarity(rng, on_holiday=on_holiday)
        is_boss = roll_is_boss(rng)
        required_hits = boss_required_hits(rarity) if is_boss else None
        expires = datetime.now(timezone.utc) + timedelta(seconds=lifetime_seconds)
        row = await self.db.fetchrow(
            "INSERT INTO duck_events (chat_id, rarity, expires_at, "
            "                         boss_required_hits) "
            "VALUES ($1, $2, $3, $4) "
            "RETURNING id, chat_id, rarity, spawned_at, expires_at, "
            "          boss_required_hits, boss_current_hits",
            chat_id, rarity, expires, required_hits,
        )
        assert row is not None
        log.info(
            "Spawned %sduck in chat %s (event id=%s).",
            "BOSS " if is_boss else "", chat_id, row["id"],
        )
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
        forced_success: bool | None = None,
    ) -> tuple[ActionOutcome, ActiveDuck] | tuple[None, None]:
        """Resolve a `bang` action against the current active duck.

        ``forced_success`` is an admin debug-toggle escape hatch
        (``always_hit`` / ``always_miss`` in :mod:`ipedro.duckhunt.debug_toggles`).
        When True/False, the dice roll is skipped and a hit/miss outcome
        is fabricated. None means "roll normally". Boss ducks ignore the
        flag — they have their own attribution flow.
        """
        duck = await self.active_duck(chat_id)
        if not duck:
            return None, None
        if duck.is_boss:
            return await self._handle_bang_boss(duck, user_id, display_name, rng)
        stats = await self.db.fetchval(
            "SELECT COALESCE(streak, 0) FROM duck_stats "
            "WHERE chat_id = $1 AND user_id = $2",
            chat_id, user_id,
        ) or 0
        if forced_success is True:
            outcome = ActionOutcome(
                success=True, points_delta=1, streak_delta=1,
                message="You shot the duck! +1 [debug: always_hit]",
                resolves_duck=True,
            )
        elif forced_success is False:
            outcome = ActionOutcome(
                success=False, points_delta=0,
                streak_delta=-int(stats),
                message="You missed! [debug: always_miss]",
                resolves_duck=False,
            )
        else:
            outcome = bang_outcome(duck.rarity, int(stats), rng)
        await self._bump_stats(chat_id, user_id, display_name, "bang", outcome)
        if outcome.resolves_duck:
            await self._resolve(duck.id, user_id, "bang", outcome.points_delta)
        return outcome, duck

    async def _handle_bang_boss(
        self, duck: ActiveDuck, user_id: int, display_name: str,
        rng: random.Random | None = None,
    ) -> tuple[ActionOutcome, ActiveDuck]:
        """A bang on a boss duck. Bosses always take the hit; killing blow
        awards a bonus to the killer; everyone who contributed gets credit."""
        from ipedro.duckhunt.scoring import ActionOutcome  # local import
        # Increment boss hit counter & per-user attribution row.
        await self.db.execute(
            """
            INSERT INTO duck_boss_hits (duck_id, user_id, display_name, hits)
            VALUES ($1, $2, $3, 1)
            ON CONFLICT (duck_id, user_id) DO UPDATE SET
                hits = duck_boss_hits.hits + 1,
                display_name = EXCLUDED.display_name
            """,
            duck.id, user_id, display_name,
        )
        new_hits = await self.db.fetchval(
            "UPDATE duck_events SET boss_current_hits = boss_current_hits + 1 "
            " WHERE id = $1 RETURNING boss_current_hits",
            duck.id,
        )
        required = duck.boss_required_hits or 1
        small_pts = max(1, base_points(duck.rarity) // 4)
        if new_hits >= required:
            # Killing blow.
            big_pts = base_points(duck.rarity) * 2
            outcome = ActionOutcome(
                success=True,
                points_delta=big_pts,
                streak_delta=1,
                message=(
                    f"BOOM. {display_name} lands the killing blow on the "
                    f"BOSS duck! +{big_pts} bonus."
                ),
                resolves_duck=True,
            )
            # Award the killer.
            await self._bump_stats(
                duck.chat_id, user_id, display_name, "bang", outcome,
            )
            # Award everyone who hit at all (small participation points).
            contributors = await self.db.fetch(
                "SELECT user_id, display_name, hits FROM duck_boss_hits "
                "WHERE duck_id = $1",
                duck.id,
            )
            for c in contributors:
                if c["user_id"] == user_id:
                    continue
                share_outcome = ActionOutcome(
                    success=True,
                    points_delta=small_pts * int(c["hits"]),
                    streak_delta=0,
                    message="",
                    resolves_duck=False,
                )
                await self._bump_stats(
                    duck.chat_id, c["user_id"], c["display_name"],
                    "bang", share_outcome,
                )
            await self._resolve(
                duck.id, user_id, "bang", outcome.points_delta,
            )
            return outcome, duck
        # Not yet down.
        progress = f"{new_hits}/{required}"
        outcome = ActionOutcome(
            success=True,
            points_delta=small_pts,
            streak_delta=0,
            message=(
                f"💥 Hit on the BOSS duck "
                f"({progress}). +{small_pts}"
            ),
            resolves_duck=False,
        )
        await self._bump_stats(
            duck.chat_id, user_id, display_name, "bang", outcome,
        )
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
        if duck.is_boss:
            from ipedro.duckhunt.scoring import ActionOutcome
            return (
                ActionOutcome(
                    success=False, points_delta=0, streak_delta=0,
                    message=(
                        "This duck is too big to befriend. You'd be eaten. "
                        "Try `bang`."
                    ),
                    resolves_duck=False,
                ),
                duck,
            )

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

    async def global_leaderboard(self, limit: int = 15) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT user_id,
                   MAX(display_name) AS display_name,
                   SUM(points)       AS points,
                   SUM(killed)       AS killed,
                   SUM(befriended)   AS befriended,
                   MAX(best_streak)  AS best_streak,
                   COUNT(DISTINCT chat_id) AS chats
              FROM duck_stats
             GROUP BY user_id
             ORDER BY points DESC NULLS LAST
             LIMIT $1
            """,
            limit,
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
            SELECT id, rarity, resolved_at, name
              FROM duck_events
             WHERE chat_id = $1 AND resolved_by = $2 AND resolved_action = 'bef'
             ORDER BY resolved_at DESC
             LIMIT $3
            """,
            chat_id, user_id, limit,
        )
        return [dict(r) for r in rows]

    async def name_duck(
        self, chat_id: int, user_id: int, duck_id: int, name: str,
    ) -> bool:
        """Set the name on a duck the caller has befriended. Returns False if not theirs."""
        res = await self.db.execute(
            """
            UPDATE duck_events SET name = $1
             WHERE id = $2 AND chat_id = $3
               AND resolved_by = $4 AND resolved_action = 'bef'
            """,
            name, duck_id, chat_id, user_id,
        )
        try:
            return int(res.split()[-1]) > 0
        except Exception:
            return False

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

    async def clear_all_bef_challenges(self, chat_id: int) -> int:
        """Drop every pending bef challenge in a chat. Returns the count.

        Admin escape hatch (``/debug_clear_challenge``) for when a stuck
        challenge is gating a chat — most acutely a DM, where the
        interceptor otherwise judges every message as a failed answer."""
        res = await self.db.execute(
            "DELETE FROM bef_challenges WHERE chat_id = $1", chat_id,
        )
        try:
            return int(res.split()[-1])
        except (AttributeError, ValueError, IndexError):
            return 0

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
