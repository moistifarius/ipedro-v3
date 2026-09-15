"""Tests for the /manage admin hub keyboards and the leaf manifest.

The hub is generated from ipedro.handlers.command_catalog so these tests
walk the catalog rather than hand-rolled lists. Adding a new command
row in the catalog is enough to make it show up in the hub; the leaf
manifest derives automatically.
"""

from __future__ import annotations

from ipedro.handlers.admin import (
    _MGM_LEAVES,
    _mgm_ai_submenu,
    _mgm_category_submenu,
    _mgm_chats_submenu,
    _mgm_debug_submenu,
    _mgm_duck_submenu,
    _mgm_memory_submenu,
    _mgm_top_keyboard,
)
from ipedro.handlers.command_catalog import (
    CATEGORIES, COMMANDS, categories_in_order, commands_in_category,
)


def _all_callbacks(kb) -> list[str]:
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_mgm_top_keyboard_has_every_catalog_category():
    """The top menu surfaces every category in the catalog, in catalog
    order. Legacy admin categories keep their short callback names
    (mgm:memory etc.) for backwards compat."""
    kb = _mgm_top_keyboard()
    callbacks = _all_callbacks(kb)
    # One button per category, in declaration order.
    assert len(callbacks) == len(CATEGORIES)
    # The original five admin categories keep their original short names
    # so an old dispatch path doesn't break.
    assert "mgm:memory" in callbacks
    assert "mgm:duck" in callbacks
    assert "mgm:ai" in callbacks
    assert "mgm:chats" in callbacks
    assert "mgm:debug" in callbacks
    # New categories are reachable via mgm:cat:<key>.
    for cat in CATEGORIES:
        if cat.key in {"memory", "duckhunt", "ai_admin", "chats", "debug"}:
            continue
        assert f"mgm:cat:{cat.key}" in callbacks


def test_mgm_memory_submenu_callbacks():
    callbacks = _all_callbacks(_mgm_memory_submenu())
    # Wired admin tools must still resolve to their pickers.
    for c in (
        "mgm:memory:facts", "mgm:memory:stats", "mgm:memory:summary",
        "mgm:memory:force", "mgm:top",
    ):
        assert c in callbacks


def test_mgm_duck_submenu_callbacks():
    callbacks = _all_callbacks(_mgm_duck_submenu())
    for c in (
        "mgm:duck:edit", "mgm:duck:reset", "mgm:duck:spawn",
        "mgm:duck:spawnall", "mgm:top",
    ):
        assert c in callbacks


def test_mgm_ai_submenu_callbacks():
    callbacks = _all_callbacks(_mgm_ai_submenu())
    assert "mgm:ai:show" in callbacks
    assert "mgm:top" in callbacks
    # Cross-links into aip:* dispatcher.
    assert "aip:switch:claude" in callbacks
    assert "aip:switch:openai" in callbacks


def test_mgm_chats_submenu_callbacks():
    callbacks = _all_callbacks(_mgm_chats_submenu())
    for c in ("mgm:chats:list", "mgm:chats:pick", "mgm:top"):
        assert c in callbacks


def test_mgm_debug_submenu_callbacks():
    callbacks = _all_callbacks(_mgm_debug_submenu())
    for c in (
        "mgm:debug:status", "mgm:debug:logs", "mgm:debug:cost",
        "mgm:debug:cmdlog", "mgm:top",
    ):
        assert c in callbacks


def test_every_mgm_leaf_callback_in_manifest():
    """All mgm: callback_datas emitted by the hub — including every
    catalog-generated card and category callback — must appear in
    _MGM_LEAVES so the dispatcher knows about them."""
    all_callbacks: set[str] = set()
    builders = [_mgm_top_keyboard]
    for cat in categories_in_order():
        builders.append(lambda key=cat.key: _mgm_category_submenu(key))
    for builder in builders:
        all_callbacks.update(_all_callbacks(builder()))
    mgm_only = {c for c in all_callbacks if c.startswith("mgm:")}
    leaves = set(_MGM_LEAVES)
    missing = mgm_only - leaves
    assert not missing, f"mgm callbacks not in _MGM_LEAVES: {missing}"


def test_every_command_in_catalog_has_a_card_or_a_wired_action():
    """A command row either points to a wired mgm: action OR shows a
    usage card (default). Both endpoints must be in _MGM_LEAVES so
    tapping the leaf never lands the admin on a dead callback."""
    leaves = set(_MGM_LEAVES)
    for cmd in COMMANDS:
        target = cmd.action or f"mgm:cmd:{cmd.slug}"
        assert target in leaves, (
            f"{cmd.name} (slug={cmd.slug}) → {target} not in _MGM_LEAVES"
        )


def test_command_slugs_are_unique_within_the_catalog():
    """Slugs are embedded in callback data — duplicates would collide
    and surface the wrong card."""
    slugs = [c.slug for c in COMMANDS]
    assert len(slugs) == len(set(slugs)), "duplicate slug in COMMANDS"


def test_every_category_has_at_least_one_command():
    """A category with zero commands renders as an empty submenu —
    catch that at test time, not at runtime."""
    for cat in CATEGORIES:
        cmds = commands_in_category(cat.key)
        assert cmds, f"category {cat.key!r} has no commands in the catalog"
