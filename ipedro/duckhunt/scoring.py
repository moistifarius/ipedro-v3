"""Duckhunt scoring.

The codebase used to scale dice rolls, hit chances, point payouts and AI
personality by per-duck rarity tiers (common / uncommon / rare / epic /
legendary). Rarity has been **neutralized for now** — every duck behaves
identically. We still:

  * keep the RARITY_TIERS / RARITY_BY_NAME / BEF_BASE_CHANCE tables in
    place so existing imports and historical data (the duck_events.rarity
    column) keep working;
  * keep the function signatures `bang_outcome(rarity, …)`,
    `bef_dice_passes(rarity, …)`, etc. — callers still pass rarity, the
    helpers just ignore it;
  * always insert `"common"` into duck_events for new spawns.

To bring rarity back, restore the per-tier lookups in
`bang_outcome` / `bef_dice_passes` / `ignore_outcome` / `base_points`,
restore `roll_rarity`'s weighted draw, and re-add the rarity placeholder
to DUCK_BEF_DECIDE_PROMPT.

Pure functions only — kept dependency-free so they are trivial to unit test.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date

# Tiers preserved for historical compatibility. Not consulted by gameplay
# anymore; new spawns always tag as "common" via roll_rarity().
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

# Flat per-duck payout while rarity is neutralized. One point per successful
# resolve regardless of action; matches the bang_outcome / bef_success_outcome
# defaults below. Boss bonus is computed off this too.
FLAT_DUCK_POINTS = 1

# --------------------------------------------------------------- difficulty
# Tuned for "rare-event-rich" play: bang misses bite, bef refusals are
# common, and roughly a third of misses snowball into a captcha challenge
# the user has to clear before they can shoot again.
BANG_BASE_HIT = 0.65          # was 0.80 — bang misses meaningfully more
BANG_HIT_CAP = 0.85           # was 0.95 — streaks help, but ceiling lower
BANG_STREAK_BONUS = 0.02      # per streak point, capped at the streak cap
BANG_STREAK_CAP = 5

# Probability the AI's verdict gets bypassed in favour of an outright
# refusal — the duck doesn't even hear you out. 0.0 reproduces the
# previous behaviour ("dice always passes; AI decides").
BEF_REFUSE_RATE = 0.35

# When a bang misses, this is the chance the duck "spooks" you into a
# follow-up captcha/trivia/recipe challenge. The challenge gates further
# bangs until cleared (same plumbing the bef refusal uses).
MISS_CHALLENGE_RATE = 0.30

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

# Preserved for backwards compatibility; not consulted while rarity is off.
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
    """Boss takes a fixed number of hits while rarity is neutralized."""
    return 3


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
    """Rarity neutralized — every spawn is tagged 'common' regardless of
    weights or holiday status. Signature preserved for callers."""
    return "common"


def roll_is_boss(rng: random.Random | None = None) -> bool:
    r = rng if rng is not None else random
    return r.random() < BOSS_SPAWN_CHANCE


def base_points(rarity: str) -> int:
    """Flat one point per duck while rarity is neutralized."""
    return FLAT_DUCK_POINTS


def bang_outcome(rarity: str, current_streak: int, rng: random.Random | None = None) -> ActionOutcome:
    """Trying to shoot the duck. Streak gives a small accuracy bonus.
    Rarity is accepted but ignored — flat ``BANG_BASE_HIT`` base hit chance,
    rising with streak up to ``BANG_HIT_CAP``."""
    r = rng if rng is not None else random
    streak_bonus = min(current_streak, BANG_STREAK_CAP) * BANG_STREAK_BONUS
    hit_chance = min(BANG_HIT_CAP, BANG_BASE_HIT + streak_bonus)
    if r.random() <= hit_chance:
        pts = FLAT_DUCK_POINTS
        return ActionOutcome(
            success=True,
            points_delta=pts,
            streak_delta=1,
            message=f"You shot the duck! +{pts}",
            resolves_duck=True,
        )
    return ActionOutcome(
        success=False, points_delta=0, streak_delta=-current_streak,
        message="You missed!", resolves_duck=False,
    )


# --------------------------------------------------------------------- bef
# bef is a two-stage decision:
#   1) Dice pre-roll (currently always passes — rarity neutralized)
#   2) AI verdict (gates the roll — can override accept to refuse)
# The pure scoring helpers here only know about the dice and verdict; the
# AI call and the per-user retry challenge live in the service layer.

# Preserved for historical compatibility. Not consulted by bef_dice_passes.
BEF_BASE_CHANCE: dict[str, float] = {
    "common":    0.85,
    "uncommon":  0.75,
    "rare":      0.55,
    "epic":      0.35,
    "legendary": 0.15,
}


def bef_dice_passes(rarity: str, rng: random.Random | None = None) -> bool:
    """Step 1 of the bef flow.

    Returns False with probability ``BEF_REFUSE_RATE`` — the duck refuses
    outright before the AI verdict is even consulted. This is the harder
    setting: roughly one bef in three is a flat-out "no" regardless of
    AI mood.
    """
    r = rng if rng is not None else random
    return r.random() >= BEF_REFUSE_RATE


def should_challenge_on_miss(rng: random.Random | None = None) -> bool:
    """True with probability ``MISS_CHALLENGE_RATE``.

    Rolled after a bang miss to decide whether the duck spooks the
    shooter into a captcha/trivia/recipe challenge that must be cleared
    before they can bang again.
    """
    r = rng if rng is not None else random
    return r.random() < MISS_CHALLENGE_RATE


def bef_success_outcome(rarity: str, ai_line: str | None) -> ActionOutcome:
    """The duck agreed to be friends. Flat one-point payout."""
    pts = FLAT_DUCK_POINTS
    return ActionOutcome(
        success=True,
        points_delta=pts,
        streak_delta=1,
        message=ai_line or f"You befriended the duck! +{pts}",
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
    """Sometimes the duck notices and stays; otherwise it wanders off.
    Rarity is accepted but ignored — flat 0.20 notice chance."""
    r = rng if rng is not None else random
    noticed_chance = 0.20
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
