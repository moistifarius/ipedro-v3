"""Token counting helpers."""

from __future__ import annotations

from ipedro.memory.tokens import count_tokens


def test_empty_string_is_zero_tokens():
    assert count_tokens("") == 0


def test_count_tokens_is_positive_for_real_text():
    assert count_tokens("hello world, how are you today?") > 0
