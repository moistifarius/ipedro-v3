"""Chat response-policy tests."""

from __future__ import annotations

import random

from ipedro.chat_policy import IncomingMessage, should_respond


def _msg(**overrides) -> IncomingMessage:
    base = dict(
        text="hello",
        has_mention_of_bot=False,
        is_reply_to_bot=False,
        is_command=False,
        chat_type="supergroup",
    )
    base.update(overrides)
    return IncomingMessage(**base)


def test_commands_never_invoke_policy():
    assert should_respond("always", _msg(is_command=True)) is False


def test_always_policy_in_group_replies():
    assert should_respond("always", _msg()) is True


def test_commands_only_policy_never_replies_in_group():
    assert should_respond("commands", _msg()) is False
    assert should_respond("commands", _msg(has_mention_of_bot=True)) is False


def test_mention_policy_requires_mention_or_reply():
    assert should_respond("mention", _msg()) is False
    assert should_respond("mention", _msg(has_mention_of_bot=True)) is True
    assert should_respond("mention", _msg(is_reply_to_bot=True)) is True


def test_reply_policy_only_on_reply():
    assert should_respond("reply", _msg()) is False
    assert should_respond("reply", _msg(has_mention_of_bot=True)) is False
    assert should_respond("reply", _msg(is_reply_to_bot=True)) is True


def test_ambient_replies_on_explicit_trigger():
    assert should_respond("ambient", _msg(has_mention_of_bot=True)) is True


def test_ambient_uses_probability_for_other_messages():
    # Force always-true RNG
    rng = random.Random(0)
    rng.random = lambda: 0.0  # type: ignore[method-assign]
    assert should_respond("ambient", _msg(), ambient_probability=0.01, rng=rng) is True
    rng2 = random.Random(0)
    rng2.random = lambda: 0.99  # type: ignore[method-assign]
    assert should_respond("ambient", _msg(), ambient_probability=0.01, rng=rng2) is False


def test_unknown_policy_falls_back_to_mention():
    assert should_respond("bogus", _msg()) is False
    assert should_respond("bogus", _msg(has_mention_of_bot=True)) is True


def test_private_chat_defaults_to_replying():
    assert should_respond("mention", _msg(chat_type="private")) is True
    assert should_respond("commands", _msg(chat_type="private")) is False
