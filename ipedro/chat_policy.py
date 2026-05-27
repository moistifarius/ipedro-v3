"""Per-chat response policy.

Decides whether the bot should reply to a non-command message based on the
chat's configured policy.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class IncomingMessage:
    """The bits of a Telegram message the policy needs."""

    text: str
    has_mention_of_bot: bool
    is_reply_to_bot: bool
    is_command: bool
    chat_type: str  # private | group | supergroup | channel


VALID_POLICIES = {"commands", "mention", "reply", "ambient", "always"}


def should_respond(
    policy: str,
    msg: IncomingMessage,
    *,
    ambient_probability: float = 0.0,
    rng: random.Random | None = None,
) -> bool:
    """Return True if the bot should generate an AI reply to this message.

    Commands are dispatched by their own handlers and bypass this check; it's
    only consulted for ambient/free-text messages.
    """
    if msg.is_command:
        # Commands have dedicated handlers; the chat policy does not gate them.
        return False

    if policy not in VALID_POLICIES:
        policy = "mention"

    # Private chats default to 'always' if any reasonable policy is set;
    # treat 'commands' as truly opt-out.
    if msg.chat_type == "private" and policy != "commands":
        return True

    if policy == "commands":
        return False
    if policy == "always":
        return True
    if policy == "mention":
        return msg.has_mention_of_bot or msg.is_reply_to_bot
    if policy == "reply":
        return msg.is_reply_to_bot
    if policy == "ambient":
        if msg.has_mention_of_bot or msg.is_reply_to_bot:
            return True
        prob = max(0.0, min(1.0, ambient_probability))
        r = rng if rng is not None else random
        return r.random() < prob

    return False
