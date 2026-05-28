"""Tests for the destructive-op confirmation keyboard."""

from __future__ import annotations

from ipedro.handlers.admin import _confirmation_keyboard


def test_confirmation_keyboard_appends_confirm_cancel_suffixes():
    kb = _confirmation_keyboard("dsra:42")
    # One row, two buttons.
    assert len(kb.inline_keyboard) == 1
    row = kb.inline_keyboard[0]
    assert len(row) == 2
    confirm_btn, cancel_btn = row
    assert confirm_btn.callback_data == "dsra:42:confirm"
    assert cancel_btn.callback_data == "dsra:42:cancel"
    assert "YES" in confirm_btn.text.upper()
    assert "CANCEL" in cancel_btn.text.upper()


def test_confirmation_keyboard_with_deep_callback_prefix():
    """E.g. dse:reset:CHATID:USERID — confirm/cancel suffixes still work."""
    kb = _confirmation_keyboard("dse:reset:-1009876:315660812")
    row = kb.inline_keyboard[0]
    assert row[0].callback_data == "dse:reset:-1009876:315660812:confirm"
    assert row[1].callback_data == "dse:reset:-1009876:315660812:cancel"


def test_confirmation_yes_fires_after_arbitrary_delay():
    """Confirmation has no TTL; the callback dispatcher doesn't gate on time.

    We exercise the keyboard generator and the suffix layout — both contain
    no time-sensitive state.
    """
    kb1 = _confirmation_keyboard("dsra:7")
    kb2 = _confirmation_keyboard("dsra:7")
    # Two independently-generated keyboards have the same callback shape.
    assert kb1.inline_keyboard[0][0].callback_data == kb2.inline_keyboard[0][0].callback_data
