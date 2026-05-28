"""Tests for the /manage admin hub keyboards and the leaf manifest."""

from __future__ import annotations

from ipedro.handlers.admin import (
    _MGM_LEAVES,
    _mgm_ai_submenu,
    _mgm_chats_submenu,
    _mgm_debug_submenu,
    _mgm_duck_submenu,
    _mgm_memory_submenu,
    _mgm_top_keyboard,
)


def _all_callbacks(kb) -> list[str]:
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_mgm_top_keyboard_has_five_categories():
    kb = _mgm_top_keyboard()
    callbacks = _all_callbacks(kb)
    # Five top-level buttons, one per category.
    assert "mgm:memory" in callbacks
    assert "mgm:duck" in callbacks
    assert "mgm:ai" in callbacks
    assert "mgm:chats" in callbacks
    assert "mgm:debug" in callbacks
    # No more, no fewer.
    assert len(callbacks) == 5


def test_mgm_memory_submenu_callbacks():
    callbacks = _all_callbacks(_mgm_memory_submenu())
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
    """All callback_datas emitted by submenu builders must be in
    _MGM_LEAVES. The dispatcher checks against the same manifest."""
    all_callbacks: set[str] = set()
    for builder in (
        _mgm_top_keyboard, _mgm_memory_submenu, _mgm_duck_submenu,
        _mgm_ai_submenu, _mgm_chats_submenu, _mgm_debug_submenu,
    ):
        all_callbacks.update(_all_callbacks(builder()))
    # Cross-links like aip:switch:claude come from another dispatcher;
    # filter to just mgm:* entries.
    mgm_only = {c for c in all_callbacks if c.startswith("mgm:")}
    leaves = set(_MGM_LEAVES)
    missing = mgm_only - leaves
    assert not missing, f"mgm callbacks not in _MGM_LEAVES: {missing}"
