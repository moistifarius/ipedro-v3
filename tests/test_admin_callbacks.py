"""Tests for cross-link action rows and callback parsing."""

from __future__ import annotations

from ipedro.handlers.admin import (
    _aip_action_row,
    _mfx_action_row,
    _mst_action_row,
)


def test_mst_action_row_callback_shapes():
    row = _mst_action_row(-1009876543210123456)
    callback_datas = [b.callback_data for b in row]
    assert callback_datas == [
        "mst:facts:-1009876543210123456",
        "mst:force:-1009876543210123456",
        "mst:edit:-1009876543210123456",
        "mst:back",
    ]


def test_mfx_action_row_callback_shapes():
    row = _mfx_action_row(42)
    callback_datas = [b.callback_data for b in row]
    assert callback_datas == [
        "mfx:reextract:42",
        "mfx:stats:42",
        "mfx:back",
    ]


def test_aip_action_row_includes_switch_buttons():
    row = _aip_action_row()
    callback_datas = [b.callback_data for b in row]
    assert "aip:switch:claude" in callback_datas
    assert "aip:switch:openai" in callback_datas
    assert "aip:list:claude" in callback_datas
    assert "aip:list:openai" in callback_datas


def test_mst_callback_parser_recovers_negative_chatid():
    """Negative chat ids must round-trip cleanly through the verb parser."""
    cb_data = "mst:edit:-1009876543210123456"
    parts = cb_data.split(":", 2)
    assert parts[0] == "mst"
    assert parts[1] == "edit"
    assert int(parts[2]) == -1009876543210123456
