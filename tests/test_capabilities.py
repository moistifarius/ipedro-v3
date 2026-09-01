"""The capability brief: the bot is told, on every reply, what it can and
can't do, without being told it's a bot.

Regression: asked to set a reminder in prose the bot would claim it had,
asked what it could do it would guess, and asked whether it could browse
the web it would say yes. And the moment it explained any of that it
called itself an AI.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.capabilities import (
    HIDDEN_SLUGS, PUBLIC_CATEGORIES, capability_brief, is_character_persona,
    public_commands,
)
from ipedro.db.repositories import ChatConfig
from ipedro.handlers import chat
from ipedro.handlers.command_catalog import CATEGORIES, COMMANDS
from ipedro.memory.context_builder import BuiltContext
from ipedro.memory.tokens import count_tokens
from tests.test_captcha_intercept import _msg


def _cfg(**overrides) -> ChatConfig:
    base = dict(
        chat_id=1, response_policy="mention", ambient_probability=0.03,
        persona="dude", persona_custom=None, duckhunt_enabled=False,
        voice_transcribe=True, memory_enabled=True,
        monthly_recap_enabled=False,
    )
    base.update(overrides)
    return ChatConfig(**base)


# ── what's in it ─────────────────────────────────────────────────────────────

def test_every_public_command_is_in_the_brief():
    """A new public catalog row is automatically something the bot knows
    about — no second list to keep in sync."""
    brief = capability_brief(_cfg())
    for cmd in public_commands():
        assert cmd.usage in brief, cmd.slug
    assert "/remind <duration> <text>" in brief
    assert "/aigen <prompt>" in brief


def test_admin_commands_are_never_advertised():
    brief = capability_brief(_cfg())
    admin_only = ("/memory_wipe", "/send_message", "/master_prompt",
                  "/dalegif", "/quack_all", "/shutup", "/logs")
    for name in admin_only:
        assert name not in brief, name


def test_hidden_slugs_all_exist_in_the_catalog():
    """A renamed slug would silently un-hide its command; pin the names."""
    assert HIDDEN_SLUGS <= {c.slug for c in COMMANDS}


def test_public_categories_are_real_and_non_admin():
    keys = {c.key for c in CATEGORIES}
    assert set(PUBLIC_CATEGORIES) <= keys
    assert not {"mod", "memory", "chats", "ai_admin", "debug"} & set(
        PUBLIC_CATEGORIES
    )
    assert all(c.category in PUBLIC_CATEGORIES for c in public_commands())


def test_the_cannots_cover_the_usual_false_promises():
    brief = capability_brief(_cfg())
    assert "Browse the web" in brief
    assert "See photos" in brief
    assert "Set a reminder" in brief and "slash command" in brief
    assert "Never claim you did it" in brief


def test_the_brief_is_not_a_novel():
    """It rides along on every AI reply; keep it well under a sixth of the
    default 6000-token context budget."""
    fully_on = _cfg(
        duckhunt_enabled=True, monthly_recap_enabled=True,
        share_photo_enabled=True, comic_enabled=True, fortune_enabled=True,
        ether_enabled=True,
    )
    assert count_tokens(capability_brief(fully_on)) < 1000


# ── per-chat lines ───────────────────────────────────────────────────────────

def test_memory_off_is_stated_plainly():
    assert "Memory is OFF" in capability_brief(_cfg(memory_enabled=False))
    assert "Memory is OFF" not in capability_brief(_cfg(memory_enabled=True))


def test_voice_transcription_switch_is_reflected():
    assert "you hear them" in capability_brief(_cfg(voice_transcribe=True))
    assert "can't hear them" in capability_brief(_cfg(voice_transcribe=False))


def test_scheduled_posts_list_only_what_is_switched_on():
    brief = capability_brief(_cfg(duckhunt_enabled=True))
    assert "ducks" in brief and "four-panel comic" not in brief
    nothing = capability_brief(_cfg())
    assert "Nothing is scheduled here" in nothing
    assert "ducks that wander" not in nothing


def test_response_policy_is_explained():
    assert "every message here" in capability_brief(
        _cfg(response_policy="always"),
    )
    assert "only replies to your own messages" in capability_brief(
        _cfg(response_policy="reply"),
    )


# ── staying in character ─────────────────────────────────────────────────────

def test_character_persona_gets_the_not_a_bot_rule():
    brief = capability_brief(_cfg(persona="dude"))
    assert "NOT a bot" in brief
    assert "never concede" in brief


def test_neutral_persona_is_allowed_to_be_an_assistant():
    """The neutral persona literally is an assistant; telling it to deny
    that would be a lie in the other direction."""
    brief = capability_brief(_cfg(persona="neutral"))
    assert "NOT a bot" not in brief
    assert "/remind" in brief           # still gets the capabilities


def test_custom_persona_is_a_character():
    assert is_character_persona("neutral", "You are a butler named Reginald.")
    assert "NOT a bot" in capability_brief(
        _cfg(persona="neutral", persona_custom="You are a butler."),
    )


def test_no_config_gives_the_generic_brief():
    brief = capability_brief()
    assert "NOT a bot" in brief
    assert "In THIS chat" not in brief
    assert "/remind" in brief


# ── it actually reaches the model ────────────────────────────────────────────

def _reply_rt(cfg):
    """A Runtime stub that carries on_message all the way to build_context."""
    from tests.test_captcha_intercept import _rt_with
    rt = _rt_with()
    rt.chats.get_config.return_value = cfg
    rt.chats.upsert_default_config.return_value = cfg
    rt.persona_state = SimpleNamespace(
        current=AsyncMock(return_value=None),
        to_system_prompt=lambda state: "",
    )
    rt.openai = SimpleNamespace(chat=AsyncMock(return_value="sh-sha"))
    return rt


@pytest.mark.asyncio
async def test_on_message_hands_the_brief_to_the_context_builder(monkeypatch):
    cfg = _cfg(response_policy="always", memory_enabled=False)
    rt = _reply_rt(cfg)
    captured = {}

    async def fake_build_context(**kwargs):
        captured.update(kwargs)
        return BuiltContext(messages=[{"role": "user", "content": "x"}], tokens=1)

    monkeypatch.setattr(chat, "build_context", fake_build_context)
    monkeypatch.setattr(chat, "resolve_impersonation", AsyncMock(return_value=None))
    monkeypatch.setattr(chat, "_REACT_PROBABILITY", 0.0)
    monkeypatch.setattr(chat, "_DALE_GIF_PROBABILITY", 0.0)
    handler = next(h.callback for h in chat.build_router(rt).observers["message"].handlers
                   if h.callback.__name__ == "on_message")
    msg = _msg(text="dale can you remind me to call mom in an hour")
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=5))

    await handler(msg)

    brief = captured["capabilities"]
    assert "/remind <duration> <text>" in brief
    assert "NOT a bot" in brief
    assert "Memory is OFF" in brief          # built from THIS chat's config
    rt.openai.chat.assert_awaited_once()
