"""Leaf-manifest test promised by command_catalog.all_action_targets():
every catalog ``action`` value must resolve to a real branch in
admin.py's on_mgm dispatcher, so /manage never ships a wired button
that lands on 'Unknown menu item.'.

on_mgm is a closure inside build_router, so it can't be imported and
invoked without standing up a full Runtime; instead we slice its body
out of the module source and regex the dispatch chain. That's honest
for this codebase: on_mgm IS a chain of ``if data == "mgm:..."`` /
``data.startswith("mgm:...")`` checks, and the extraction guard below
fails loudly if that shape ever changes.
"""

from __future__ import annotations

import re
from pathlib import Path

import ipedro.handlers.admin as admin_module
from ipedro.handlers.admin import _mgm_cat_data
from ipedro.handlers.command_catalog import (
    CATEGORIES, COMMANDS, all_action_targets, category_by_key,
    command_by_slug,
)


def _on_mgm_source() -> str:
    src = Path(admin_module.__file__).read_text(encoding="utf-8")
    start = src.index("async def on_mgm(")
    # The dispatcher ends where the next router registration begins.
    end = src.index("@r.callback_query", start)
    return src[start:end]


def _exact_leaves() -> set[str]:
    """Every ``data == "mgm:..."`` equality branch inside on_mgm."""
    return set(re.findall(r'data == "(mgm:[^"]+)"', _on_mgm_source()))


def _prefix_leaves() -> set[str]:
    """Every ``data.startswith("mgm:...")`` prefix branch inside on_mgm."""
    return set(
        re.findall(r'data\.startswith\("(mgm:[^"]+)"\)', _on_mgm_source())
    )


def test_dispatcher_extraction_is_not_a_tautology():
    """Guard the guard: the regexes must actually recover the dispatch
    chain. If on_mgm is restructured these collapse and this test fails
    first, pointing at the extraction instead of a phantom missing leaf."""
    exact = _exact_leaves()
    assert len(exact) >= 15, f"only found {len(exact)} exact mgm: branches"
    assert "mgm:top" in exact
    assert "mgm:this:leaf:does:not:exist" not in exact
    assert {"mgm:cmd:", "mgm:cat:"} <= _prefix_leaves()


def test_every_catalog_action_resolves_to_a_real_mgm_leaf():
    """The wired half of the catalog: each ``action`` must have its own
    ``data == "..."`` branch in on_mgm."""
    exact = _exact_leaves()
    assert all_action_targets(), "catalog has no wired actions at all?"
    for action in all_action_targets():
        assert action in exact, (
            f"catalog action {action!r} has no matching branch in "
            "admin.on_mgm — tapping that button would hit "
            "'Unknown menu item.'"
        )


def test_every_card_command_routes_through_the_cmd_branch():
    """The card half: action-less rows dispatch via the mgm:cmd: prefix
    branch, which resolves the slug with command_by_slug — mirror that
    exact lookup here so a renamed slug can't orphan its card."""
    assert "mgm:cmd:" in _prefix_leaves()
    for cmd in COMMANDS:
        if cmd.action is None:
            assert command_by_slug(cmd.slug) is cmd


def test_every_category_button_resolves_in_the_dispatcher():
    """Top-menu category callbacks: legacy short names (mgm:memory,
    mgm:duck, …) dispatch via exact match; everything else via the
    mgm:cat: prefix branch + category_by_key — the dispatcher's lookup."""
    exact = _exact_leaves()
    for cat in CATEGORIES:
        data = _mgm_cat_data(cat.key)
        if data.startswith("mgm:cat:"):
            assert category_by_key(cat.key) is cat
        else:
            assert data in exact, f"legacy category leaf {data!r} missing"
