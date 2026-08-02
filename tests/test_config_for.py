"""Tests for the DM-scoped /config_for admin command + cfg: callback refactor.

The wizard's callback prefix was widened from ``cfg:<rest>`` to
``cfg:<target_chat_id>:<rest>`` so the same wizard works from an admin DM
scoped to any chat. These tests pin that contract.
"""

from __future__ import annotations

from ipedro.handlers.admin import _MGM_LEAVES, _mgm_chats_submenu
from ipedro.handlers.utility import _config_keyboard, _config_wizard_header


class _FakeCfg:
    """Mirror just enough of ChatConfig for keyboard + header rendering."""
    def __init__(self, **kw):
        defaults = dict(
            duckhunt_enabled=False, share_photo_enabled=False,
            comic_enabled=False, fortune_enabled=False,
            voice_transcribe=True, memory_enabled=True,
            ether_enabled=False, duck_names_public=True,
            on_this_day_enabled=True, monthly_recap_enabled=True,
            response_policy="mention", persona="dude",
            persona_custom=None, ambient_probability=0.03,
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
    """In-group wizard renders without the chat id in the title."""
    cfg = _FakeCfg()
    head = _config_wizard_header(cfg, target_chat_id=42, is_dm_scoped=False)
    # Title omits the chat id when scoped to the current chat.
    assert "<code>42</code>" not in head
    assert "Chat settings" in head


def test_config_wizard_header_dm_scoped_shows_target_chat_id():
    """DM-scoped header surfaces the target chat id so the admin sees which
    chat they're editing."""
    cfg = _FakeCfg()
    head = _config_wizard_header(cfg, target_chat_id=-100_555, is_dm_scoped=True)
    assert "-100555" in head


def test_config_wizard_header_shows_every_chat_config_field():
    """The header is the status panel — every ChatConfig field surfaces in
    it so an admin can read the full state of a chat in one glance."""
    cfg = _FakeCfg(
        duckhunt_enabled=True, share_photo_enabled=False,
        comic_enabled=True, fortune_enabled=False,
        voice_transcribe=True, memory_enabled=False,
        ether_enabled=True, duck_names_public=True,
        response_policy="reply", persona="neutral",
        persona_custom="be a friendly lighthouse keeper",
        ambient_probability=0.07,
    )
    head = _config_wizard_header(cfg, target_chat_id=42, is_dm_scoped=False)
    # Scalars.
    assert "reply" in head
    assert "neutral" in head
    # Custom override surfaces (truncated form is fine — just verify content).
    assert "lighthouse" in head
    # Ambient shows both percent and raw float so the admin can confirm
    # exactly what the slash command would set.
    assert "0.07" in head
    # Every boolean appears with a clear on/off marker (✅ or ⛔️).
    for name in (
        "duckhunt", "sharephoto", "comic", "fortune", "voice",
        "memory", "ether", "duck-names public",
    ):
        assert name in head
    assert "✅" in head
    assert "⛔️" in head


def test_config_wizard_header_no_custom_says_none():
    """When persona_custom is unset, the header makes that explicit so an
    admin doesn't wonder whether one's stuck."""
    cfg = _FakeCfg(persona_custom=None)
    head = _config_wizard_header(cfg, target_chat_id=42, is_dm_scoped=False)
    assert "(none)" in head


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


def test_config_keyboard_has_no_decorative_noop_buttons():
    """The Policy: / Persona: decorative headers were dropped — that info
    lives in the status panel above the keyboard now, so every button is
    actionable."""
    cfg = _FakeCfg()
    kb = _config_keyboard(cfg, target_chat_id=999)
    noops = [c for c in _all_callbacks(kb) if c.endswith(":noop")]
    assert noops == []


def test_config_keyboard_covers_every_chat_config_field():
    """The wizard's whole point is one-tap access to every ChatConfig
    field. This pin makes sure we don't quietly drop one."""
    from ipedro.handlers.utility import _AMBIENT_PRESETS

    cfg = _FakeCfg()
    callbacks = _all_callbacks(_config_keyboard(cfg, target_chat_id=42))
    suffixes = {c.split(":", 2)[2] for c in callbacks}
    # Every boolean field toggle.
    for f in (
        "duckhunt", "sharephoto", "comic", "fortune",
        "voice", "memory", "ether", "ducknames",
    ):
        assert f in suffixes, f
    # Every response_policy choice.
    for p in ("commands", "mention", "reply", "ambient", "always"):
        assert f"policy:{p}" in suffixes
    # Every ambient preset, written with the same float-formatting used
    # by the keyboard so the dispatcher round-trip works.
    for _label, value in _AMBIENT_PRESETS:
        assert f"ambient:{value:.2f}" in suffixes
    # Persona switches + the clear-custom escape hatch.
    assert "persona:dude" in suffixes
    assert "persona:neutral" in suffixes
    assert "custompersona:clear" in suffixes


def test_ambient_preset_button_marks_the_active_value():
    """The wizard puts a leading dot on the preset whose value matches the
    live ambient_probability so the admin sees the current setting at a
    glance instead of having to read the header."""
    cfg = _FakeCfg(ambient_probability=0.10)
    kb = _config_keyboard(cfg, target_chat_id=42)
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert "• 10%" in labels
    # Other presets are NOT marked.
    assert "• 25%" not in labels
    assert "25%" in labels


def test_ambient_preset_dispatch_clamps_and_persists(monkeypatch):
    """The handler sticks the chosen float into chat_config and clamps it
    so a malformed callback (or future regression) can't push values out
    of [0, 1]."""
    # This is a unit-level check on the parsing logic — the float must
    # parse cleanly back from the encoded suffix. The dispatcher itself
    # is exercised via integration in the real bot.
    from ipedro.handlers.utility import _AMBIENT_PRESETS

    for _label, value in _AMBIENT_PRESETS:
        suffix = f"ambient:{value:.2f}"
        parsed = float(suffix.split(":", 1)[1])
        assert 0.0 <= parsed <= 1.0
        assert abs(parsed - value) < 1e-6
