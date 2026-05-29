"""Tests for the DM-scoped /config_for admin command + cfg: callback refactor.

The wizard's callback prefix was widened from ``cfg:<rest>`` to
``cfg:<target_chat_id>:<rest>`` so the same wizard works from an admin DM
scoped to any chat. These tests pin that contract.
"""

from __future__ import annotations

from ipedro.handlers.admin import _MGM_LEAVES, _mgm_chats_submenu
from ipedro.handlers.utility import _config_keyboard, _config_wizard_header


class _FakeCfg:
    """Mirror just enough of ChatConfig for keyboard rendering."""
    def __init__(self, **kw):
        defaults = dict(
            duckhunt_enabled=False, share_photo_enabled=False,
            comic_enabled=False, fortune_enabled=False,
            voice_transcribe=True, memory_enabled=True,
            response_policy="mention", persona="dude",
        )
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


def _all_callbacks(kb) -> list[str]:
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_config_keyboard_encodes_target_chat_id_in_every_callback():
    """Every cfg button must carry the target chat id so the handler edits
    the RIGHT chat, not whichever chat hosts the wizard message."""
    cfg = _FakeCfg()
    target = -100_999_888
    kb = _config_keyboard(cfg, target_chat_id=target)
    callbacks = _all_callbacks(kb)
    # All callbacks are cfg:<chat_id>:<rest>.
    for cb in callbacks:
        assert cb.startswith("cfg:"), cb
        parts = cb.split(":", 2)
        assert len(parts) == 3, f"Expected 3 colon-separated parts in {cb!r}"
        assert int(parts[1]) == target, (
            f"Callback {cb!r} encodes wrong chat id"
        )


def test_config_keyboard_distinct_target_chat_ids_isolated():
    """Two keyboards with different target_chat_ids must not share callbacks."""
    cfg = _FakeCfg()
    kb_a = _config_keyboard(cfg, target_chat_id=111)
    kb_b = _config_keyboard(cfg, target_chat_id=222)
    cbs_a = set(_all_callbacks(kb_a))
    cbs_b = set(_all_callbacks(kb_b))
    # No overlap whatsoever — each callback uniquely identifies its target.
    assert not (cbs_a & cbs_b)


def test_config_wizard_header_in_group_is_unscoped():
    """In-group wizard renders the original short header — no chat id."""
    cfg = _FakeCfg()
    head = _config_wizard_header(cfg, target_chat_id=42, is_dm_scoped=False)
    assert "42" not in head
    assert "settings" in head.lower()


def test_config_wizard_header_dm_scoped_shows_target_chat_id():
    """DM-scoped header surfaces the target chat id so the admin sees which
    chat they're editing."""
    cfg = _FakeCfg()
    head = _config_wizard_header(cfg, target_chat_id=-100_555, is_dm_scoped=True)
    assert "-100555" in head


def test_callback_routes_to_target_chat_id_not_message_chat_id():
    """Parsing a cfg: callback yields the target chat id directly, never
    falls back to wherever the message lives."""
    cfg = _FakeCfg()
    kb = _config_keyboard(cfg, target_chat_id=-100_777)
    duckhunt_cb = next(
        c for c in _all_callbacks(kb) if c.endswith(":duckhunt")
    )
    # Shape: cfg:<chat>:<field>. Handler must read parts[1] for the chat
    # and parts[2] for the field — NOT use cb.message.chat.id.
    parts = duckhunt_cb.split(":", 2)
    assert parts[0] == "cfg"
    assert int(parts[1]) == -100_777
    assert parts[2] == "duckhunt"


def test_mgm_chats_submenu_has_configure_button():
    """The /manage → Chats submenu exposes the DM-config flow."""
    callbacks = _all_callbacks(_mgm_chats_submenu())
    assert "mgm:chats:config" in callbacks


def test_mgm_chats_config_in_leaf_manifest():
    """The dispatcher manifest lists the new leaf, so the completeness
    test in tests/test_manage.py still passes."""
    assert "mgm:chats:config" in _MGM_LEAVES


def test_config_keyboard_noop_buttons_also_carry_chat_id():
    """The Policy: / Persona: header buttons are no-ops but must still
    encode the chat id so the callback shape is uniform — the handler
    parses every cfg: callback the same way."""
    cfg = _FakeCfg()
    kb = _config_keyboard(cfg, target_chat_id=999)
    noops = [c for c in _all_callbacks(kb) if c.endswith(":noop")]
    assert len(noops) == 2  # Policy + Persona header rows
    for cb in noops:
        parts = cb.split(":", 2)
        assert parts[0] == "cfg"
        assert int(parts[1]) == 999
        assert parts[2] == "noop"
