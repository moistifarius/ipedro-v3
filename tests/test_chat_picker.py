"""Tests for the paginated chat-picker keyboard and sticky last-pick."""

from __future__ import annotations

import time

import pytest

from ipedro.handlers.admin import (
    _LAST_PICKED_CHAT,
    _chat_picker,
    _parse_picker_cb,
)


def _chats(n: int) -> list[dict]:
    return [
        {"chat_id": -100_000_000 - i, "type": "supergroup", "title": f"c{i}"}
        for i in range(n)
    ]


def _row_callbacks(kb):
    return [
        [btn.callback_data for btn in row]
        for row in kb.inline_keyboard
    ]


def test_chat_picker_single_page_when_few_chats():
    kb = _chat_picker(_chats(5), "mfacts", paginate=True)
    # No page row -> exactly 5 rows.
    assert len(kb.inline_keyboard) == 5
    # No row contains "p:" callbacks.
    for row in _row_callbacks(kb):
        for cb in row:
            assert ":p:" not in cb


def test_chat_picker_exactly_20_no_page_row():
    kb = _chat_picker(_chats(20), "mfacts", paginate=True)
    assert len(kb.inline_keyboard) == 20
    for row in _row_callbacks(kb):
        for cb in row:
            assert ":p:" not in cb


def test_chat_picker_21_creates_second_page():
    kb = _chat_picker(_chats(21), "mfacts", paginate=True, page=0)
    # 20 body rows + 1 pagination row
    assert len(kb.inline_keyboard) == 21
    last_row = kb.inline_keyboard[-1]
    assert len(last_row) == 3
    # First button is "·" (no prev on page 0).
    assert last_row[0].text == "·"
    # Middle is "1/2".
    assert last_row[1].text == "1/2"
    # Right is "next →" with mfacts:p:1 callback.
    assert last_row[2].text == "next →"
    assert last_row[2].callback_data == "mfacts:p:1"


def test_chat_picker_pages_at_45():
    # 45 chats -> 3 pages, page 2 (zero-indexed) shows 5 rows + pagination.
    kb = _chat_picker(_chats(45), "mfacts", paginate=True, page=2)
    assert len(kb.inline_keyboard) == 6  # 5 chats + pagination row
    last_row = kb.inline_keyboard[-1]
    assert last_row[1].text == "3/3"
    # No next button on last page.
    assert last_row[2].text == "·"
    # Prev button visible.
    assert last_row[0].text == "← prev"
    assert last_row[0].callback_data == "mfacts:p:1"


def test_chat_picker_zero_chats_returns_none():
    assert _chat_picker([], "mfacts", paginate=True) is None
    assert _chat_picker([], "mfacts", paginate=False) is None


def test_chat_picker_sticky_only_on_page_0_when_paginated():
    _LAST_PICKED_CHAT.clear()
    chats = _chats(25)
    # Pin a known chat in the list.
    pinned_id = chats[3]["chat_id"]
    _LAST_PICKED_CHAT[42] = (pinned_id, time.time())
    # Page 0 includes the sticky row.
    kb0 = _chat_picker(
        chats, "mfacts", paginate=True, page=0, admin_user_id=42,
    )
    assert kb0.inline_keyboard[0][0].text.startswith("⭐")
    # Page 1 does not.
    kb1 = _chat_picker(
        chats, "mfacts", paginate=True, page=1, admin_user_id=42,
    )
    for row in kb1.inline_keyboard:
        assert not row[0].text.startswith("⭐")
    _LAST_PICKED_CHAT.clear()


def test_chat_picker_sticky_filtered_when_chat_no_longer_in_membership():
    _LAST_PICKED_CHAT.clear()
    chats = _chats(5)
    # Pin an id that's NOT in the list.
    _LAST_PICKED_CHAT[42] = (-999_999, time.time())
    kb = _chat_picker(chats, "mfacts", paginate=False, admin_user_id=42)
    # No sticky row.
    for row in kb.inline_keyboard:
        assert not row[0].text.startswith("⭐")
    _LAST_PICKED_CHAT.clear()


def test_pagination_callback_shape():
    kb = _chat_picker(_chats(50), "mstats", paginate=True, page=1)
    last_row = kb.inline_keyboard[-1]
    # First button should be "mstats:p:0".
    assert last_row[0].callback_data == "mstats:p:0"
    # Last button should be "mstats:p:2".
    assert last_row[2].callback_data == "mstats:p:2"


def test_last_picked_chat_expires_after_10_min():
    _LAST_PICKED_CHAT.clear()
    chats = _chats(5)
    pinned_id = chats[2]["chat_id"]
    # Set a stale timestamp (older than 600s).
    _LAST_PICKED_CHAT[42] = (pinned_id, time.time() - 700)
    kb = _chat_picker(chats, "mfacts", paginate=False, admin_user_id=42)
    for row in kb.inline_keyboard:
        assert not row[0].text.startswith("⭐")
    _LAST_PICKED_CHAT.clear()


def test_parse_picker_cb_action_chatid():
    assert _parse_picker_cb("mfacts:-100123") == ("pick", -100123)


def test_parse_picker_cb_action_page():
    assert _parse_picker_cb("mfacts:p:2") == ("page", 2)


def test_parse_picker_cb_malformed_returns_none():
    assert _parse_picker_cb("mfacts") is None
    assert _parse_picker_cb("mfacts:abc") is None
    assert _parse_picker_cb("mfacts:p:xyz") is None
