"""Admin-scoped debug toggles for duckhunt.

Toggles are stored in ``kv_store`` keyed by admin user_id so testing in
shared chats doesn't affect other admins. The in-memory cache is read
on every duck interaction (hot path); writes both update the cache and
persist to ``kv_store`` so the state survives restarts (loaded back via
``load_all`` at startup).

Supported toggles (all default-off):

* ``always_hit``               — bang/ignore: force a successful shot
* ``always_miss``              — bang/ignore: force a miss
* ``always_pass_challenge``    — bef-challenge judge: short-circuit to PASS
* ``always_fail_challenge``    — bef-challenge judge: short-circuit to FAIL
* ``always_refuse_bef``        — bef: force the AI verdict to REFUSE
* ``bypass_cooldowns``         — bang/bef/ignore: skip the cooldown check

Each toggle is scoped to a single admin user_id; non-admin users (and
admins who haven't enabled the toggle) see normal behavior.
"""

from __future__ import annotations

from typing import Iterable

from ipedro.db.pool import Database
from ipedro.kv import kv_delete, kv_get, kv_set

# Order here matches what ``/debug_toggle show`` prints; keep
# alphabetised-ish so the panel is predictable.
DEBUG_TOGGLE_NAMES: tuple[str, ...] = (
    "always_hit",
    "always_miss",
    "always_pass_challenge",
    "always_fail_challenge",
    "always_refuse_bef",
    "bypass_cooldowns",
)

# (admin_user_id, toggle_name) -> bool. Module-level so all callers share
# the same view; populated at startup by ``load_all`` and mutated by
# ``set_toggle``.
_cache: dict[tuple[int, str], bool] = {}


def _key(admin_id: int, name: str) -> str:
    """KV store key for one (admin, toggle) pair."""
    return f"debug:{name}:{admin_id}"


async def load_all(db: Database, admin_ids: Iterable[int]) -> None:
    """Prime the in-memory cache from ``kv_store`` at startup.

    Reads every (admin_id, toggle) pair and caches the result. Missing
    rows are treated as off, matching the ``is_on`` default. Safe to
    call repeatedly (idempotent).
    """
    for admin_id in admin_ids:
        for name in DEBUG_TOGGLE_NAMES:
            val = await kv_get(db, _key(admin_id, name))
            _cache[(admin_id, name)] = (val == "1")


def is_on(admin_id: int | None, name: str) -> bool:
    """Check a toggle. Defaults to False if no entry exists.

    Accepts ``None`` for admin_id so callers can pass
    ``msg.from_user.id if msg.from_user else None`` without a guard.
    """
    if admin_id is None:
        return False
    return _cache.get((admin_id, name), False)


async def set_toggle(
    db: Database, admin_id: int, name: str, on: bool,
) -> None:
    """Flip one toggle and persist it.

    Updates the in-memory cache first (so the change is visible to the
    very next is_on call even if the kv write is slow), then writes to
    or deletes the kv_store row.
    """
    if name not in DEBUG_TOGGLE_NAMES:
        raise ValueError(f"unknown toggle: {name}")
    _cache[(admin_id, name)] = on
    if on:
        await kv_set(db, _key(admin_id, name), "1")
    else:
        await kv_delete(db, _key(admin_id, name))


def all_for(admin_id: int) -> dict[str, bool]:
    """Return the full toggle state for one admin as a dict.

    Used by ``/debug_toggle show`` to print the current panel.
    """
    return {name: is_on(admin_id, name) for name in DEBUG_TOGGLE_NAMES}


def _reset_cache_for_tests() -> None:
    """Test helper: wipe the in-memory cache between tests.

    Not part of the public API; tests use this to isolate cases.
    """
    _cache.clear()
