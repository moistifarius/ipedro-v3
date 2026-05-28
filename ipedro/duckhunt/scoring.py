"""Duckhunt scoring and rarity tables.

Pure functions only - kept dependency-free so they are trivial to unit test.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date

# Rarity tiers, in roll order. Probabilities are sampled by `roll_rarity`.
RARITY_TIERS: tuple[tuple[str, float, int], ...] = (
    # (name, weight, base_points)
    ("common",    0.65, 1),
    ("uncommon",  0.20, 3),
    ("rare",      0.10, 7),
    ("epic",      0.04, 15),
    ("legendary", 0.01, 40),
)

RARITY_BY_NAME: dict[str, tuple[float, int]] = {
    name: (weight, base_points) for name, weight, base_points in RARITY_TIERS
}

# (month, day) -> (event name, hint flavor used in the quack line)
HOLIDAYS: dict[tuple[int, int], tuple[str, str]] = {
    (1, 1):   ("New Year",         "🎆 the duck wears tiny party glasses"),
    (2, 14):  ("Valentine's",      "💌 the duck has a little rose"),
    (3, 17):  ("St. Patrick's",    "🍀 the duck is suspiciously green"),
    (4, 1):   ("April Fool's",     "🤡 is this even a real duck?"),
    (10, 31): ("Halloween",        "🎃 the duck is wearing a tiny ghost sheet"),
    (11, 11): ("Veterans' Day",    "the duck salutes"),
    (12, 24): ("Christmas Eve",    "🎄 the duck has bells"),
    (12, 25): ("Christmas",        "🎅 the duck wears a tiny santa hat"),
    (12, 31): ("New Year's Eve",   "🥂 the duck is holding a tiny glass"),
}

# On holidays, biased rarity weights instead of the defaults: more epics and
# legendaries, fewer commons. Re-normalised at use time.
HOLIDAY_WEIGHTS: tuple[tuple[str, float, int], ...] = (
    ("common",    0.40, 1),
    ("uncommon",  0.25, 3),
    ("rare",      0.20, 7),
    ("epic",      0.10, 15),
    ("legendary", 0.05, 40),
)

# Chance any given spawn is upgraded to a boss duck.
BOSS_SPAWN_CHANCE = 0.03


def current_holiday(today: date | None = None) -> tuple[str, str] | None:
    d = today or date.today()
    return HOLIDAYS.get((d.month, d.day))


def boss_required_hits(rarity: str) -> int:
    """Boss takes more hits the rarer it is. Clamped to a usable range."""
    return max(3, min(15, base_points(rarity) // 2 + 3))


@dataclass(frozen=True)
class ActionOutcome:
    success: bool
    points_delta: int
    streak_delta: int  # +1 on success, -current on miss (handled in service)
    message: str
    resolves_duck: bool  # True if the duck event should now be marked resolved


def roll_rarity(
    rng: random.Random | None = None, *, on_holiday: bool = False,
) -> str:
    r = rng if rng is not None else random
    tiers = HOLIDAY_WEIGHTS if on_holiday else RARITY_TIERS
    roll = r.random()
    cumulative = 0.0
    for name, weight, _ in tiers:
        cumulative += weight
        if roll <= cumulative:
            return name
    return tiers[-1][0]


def roll_is_boss(rng: random.Random | None = None) -> bool:
    r = rng if rng is not None else random
    return r.random() < BOSS_SPAWN_CHANCE


def base_points(rarity: str) -> int:
    return RARITY_BY_NAME.get(rarity, (0.0, 1))[1]


def bang_outcome(rarity: str, current_streak: int, rng: random.Random | None = None) -> ActionOutcome:
    """Trying to shoot the duck. Streak gives a small accuracy bonus."""
    r = rng if rng is not None else random
    # Higher rarity is harder to hit. Streak gives a tiny bonus, capped.
    hit_chance = {
        "common":    0.80,
        "uncommon":  0.70,
        "rare":      0.55,
        "epic":      0.40,
        "legendary": 0.25,
    }.get(rarity, 0.70)
    hit_chance = min(0.95, hit_chance + min(current_streak, 5) * 0.02)
    if r.random() <= hit_chance:
        pts = base_points(rarity)
        return ActionOutcome(
            success=True,
            points_delta=pts,
            streak_delta=1,
            message=f"You shot the {rarity} duck! +{pts}",
            resolves_duck=True,
        )
    return ActionOutcome(
        success=False, points_delta=0, streak_delta=-current_streak,
        message="You missed!", resolves_duck=False,
    )


# --------------------------------------------------------------------- bef
# bef is now a two-stage decision:
#   1) Dice pre-roll (rarity-biased)
#   2) AI verdict (gates the roll - can override accept to refuse)
# The pure scoring helpers here only know about the dice and verdict; the
# AI call and the per-user retry challenge live in the service layer.

BEF_BASE_CHANCE: dict[str, float] = {
    "common":    0.85,
    "uncommon":  0.75,
    "rare":      0.55,
    "epic":      0.35,
    "legendary": 0.15,
}


def bef_dice_passes(rarity: str, rng: random.Random | None = None) -> bool:
    """Step 1 of the bef flow. True means the AI gets to weigh in."""
    r = rng if rng is not None else random
    return r.random() <= BEF_BASE_CHANCE.get(rarity, 0.7)


def bef_success_outcome(rarity: str, ai_line: str | None) -> ActionOutcome:
    """The duck agreed to be friends."""
    pts = max(1, base_points(rarity) // 2 + 1)
    return ActionOutcome(
        success=True,
        points_delta=pts,
        streak_delta=1,
        message=ai_line or f"You befriended the {rarity} duck! +{pts}",
        resolves_duck=True,
    )


def bef_refusal_outcome(ai_line: str | None) -> ActionOutcome:
    """The duck declined. Per spec the duck STAYS and the user gets no stat hit."""
    return ActionOutcome(
        success=False,
        points_delta=0,
        streak_delta=0,
        message=ai_line or "Hmm. The duck doesn't want to be friends right now.",
        resolves_duck=False,
    )


# --------------------------------------------------------------------- ignore
# Ignoring usually means the duck wanders off, but per spec it sometimes
# notices the slight and reacts. Outcome is still no-points either way.
_IGNORE_NOTICED_FLAVOR: tuple[str, ...] = (
    "You try to ignore the duck. It notices, and stares at you. Awkward.",
    "You pretend the duck isn't there. The duck waddles closer, judging.",
    "You ignore the duck. It quacks once, pointedly, and continues to exist.",
    "You look away. The duck does NOT look away. You can feel it.",
)

_IGNORE_WANDER_FLAVOR: tuple[str, ...] = (
    "You ignore the duck. It wanders off.",
    "You ignore the duck. It loses interest and waddles away.",
    "The duck shrugs (somehow) and flies off.",
)


def ignore_outcome(rarity: str, rng: random.Random | None = None) -> ActionOutcome:
    """Sometimes the duck notices and stays; otherwise it wanders off."""
    r = rng if rng is not None else random
    # Rarer ducks are more likely to take offence at being ignored.
    noticed_chance = {
        "common":    0.15,
        "uncommon":  0.25,
        "rare":      0.40,
        "epic":      0.55,
        "legendary": 0.75,
    }.get(rarity, 0.20)
    if r.random() <= noticed_chance:
        return ActionOutcome(
            success=True,
            points_delta=0,
            streak_delta=0,
            message=r.choice(_IGNORE_NOTICED_FLAVOR),
            resolves_duck=False,  # duck stays - it's making a point
        )
    return ActionOutcome(
        success=True,
        points_delta=0,
        streak_delta=0,
        message=r.choice(_IGNORE_WANDER_FLAVOR),
        resolves_duck=True,
    )
