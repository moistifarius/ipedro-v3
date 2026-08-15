"""Helpers shared across handler modules."""

from __future__ import annotations

import logging
import random
import re
from typing import Iterable

from aiogram.types import Message

from ipedro.auth import AuthContext, is_admin
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

_CAT_TO_PUSSY_RE = re.compile(r"\bcats?\b", re.IGNORECASE)


def _cat_sub(m: re.Match[str]) -> str:
    word = m.group(0)
    repl = "pussies" if word.lower().endswith("s") else "pussy"
    if word.isupper():
        return repl.upper()
    if word[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def catify(text: str) -> str:
    """Replace standalone 'cat' / 'cats' with 'pussy' / 'pussies', preserving case."""
    return _CAT_TO_PUSSY_RE.sub(_cat_sub, text)


# Static dubious cat facts, used when the AI cheap model is unavailable so the
# cat feature never degrades to a bare emoji. Each mentions 'cat'/'cats' so
# catify() still turns it into the joke. Kept deliberately shaky ("probably").
_DUBIOUS_CAT_FACTS: tuple[str, ...] = (
    "a cat's purr vibrates at the exact frequency that heals broken bones. probably.",
    "cats can't taste sweetness, which is why they judge you for eating cake.",
    "a cat has 32 muscles in each ear, all dedicated to ignoring you.",
    "cats sleep about 16 hours a day because being that mysterious is exhausting.",
    "a group of cats is called a clowder, but they prefer 'the syndicate'.",
    "a cat's nose print is unique, like a fingerprint, but smugger.",
    "a cat can rotate its ears 180 degrees to better pretend it didn't hear you.",
    "cats have a third eyelid, mostly for dramatic slow blinks.",
    "a cat's whiskers are as wide as its body, which is how it knows you overfed it.",
    "cats knock things off tables to test gravity. it still works every time.",
    "a cat can make over 100 sounds; a dog manages about 10. cats are chattier gossips.",
    "the oldest known pet cat was buried with its human 9,500 years ago, still unimpressed.",
    "cats spend a third of their waking hours grooming and the rest plotting.",
    "a cat's brain is about 90% similar to a human's, which explains the contempt.",
    "a cat always lands on its feet unless it senses you're filming, out of spite.",
)


def fallback_cat_fact(rng: random.Random | None = None) -> str:
    """A random static cat fact for when the AI can't produce one."""
    return (rng or random).choice(_DUBIOUS_CAT_FACTS)


def auth_ctx(message: Message) -> AuthContext:
    return AuthContext(
        user_id=message.from_user.id if message.from_user else None,
        chat_type=message.chat.type if message.chat else "private",
    )


async def require_admin(message: Message, admin_ids: Iterable[int]) -> bool:
    """Enforce admin-only command. Returns True if the caller is allowed.

    Silently logs and does not respond when used in a group, to avoid teaching
    bystanders that admin commands exist on this bot.
    """
    ctx = auth_ctx(message)
    if not is_admin(ctx, admin_ids):
        if ctx.chat_type != "private":
            log.warning(
                "Refused admin command %r from user %s in chat %s (%s)",
                message.text, ctx.user_id, message.chat.id if message.chat else "?",
                ctx.chat_type,
            )
            return False
        await message.reply("This command is admin-only.")
        return False
    return True


async def get_or_create_chat_config(rt: Runtime, message: Message):
    """Ensure a chat is registered and return its config row."""
    assert message.chat is not None
    await rt.chats.upsert_chat(
        message.chat.id,
        message.chat.type,
        message.chat.title,
    )
    if message.from_user:
        await rt.users.upsert_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
            message.from_user.last_name,
            message.from_user.is_bot,
        )
    cfg = await rt.chats.get_config(message.chat.id)
    if cfg is None:
        s = rt.settings
        default_policy = (
            s.default_response_policy_private
            if message.chat.type == "private"
            else s.default_response_policy_group
        )
        cfg = await rt.chats.upsert_default_config(
            message.chat.id,
            response_policy=default_policy,
            ambient_probability=s.default_ambient_probability,
            persona=s.default_persona,
            duckhunt_enabled=s.duckhunt_enabled_by_default,
        )
    return cfg


def display_name(user) -> str:
    if not user:
        return "anonymous"
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    return name or (user.username or f"user{user.id}")
