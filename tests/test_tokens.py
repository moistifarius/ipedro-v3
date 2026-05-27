"""Token counting helpers."""

from __future__ import annotations

from ipedro.memory.tokens import count_messages_tokens, count_tokens


def test_empty_string_is_zero_tokens():
    assert count_tokens("") == 0


def test_count_tokens_is_positive_for_real_text():
    assert count_tokens("hello world, how are you today?") > 0


def test_count_messages_sums_content():
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "there friend"},
    ]
    assert count_messages_tokens(msgs) == count_tokens("hello") + count_tokens("there friend")


def test_messages_without_content_are_safe():
    assert count_messages_tokens([{"role": "system"}]) == 0
