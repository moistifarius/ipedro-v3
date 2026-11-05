from ipedro.personas import DEFAULT_DUDE_PROMPT, PERSONAS, resolve_persona


def test_resolve_known_persona():
    assert resolve_persona("dude", None) == DEFAULT_DUDE_PROMPT
    assert resolve_persona("neutral", None) == PERSONAS["neutral"]


def test_legacy_pedro_key_maps_to_dude():
    assert resolve_persona("pedro", None) == DEFAULT_DUDE_PROMPT


def test_resolve_unknown_falls_back_to_dude():
    assert resolve_persona("does-not-exist", None) == DEFAULT_DUDE_PROMPT


def test_custom_persona_overrides():
    custom = "You are a butler named Reginald."
    assert resolve_persona("dude", custom) == custom


def test_master_prompt_override():
    from ipedro.personas import set_master_prompt_override
    override = "You are a stoic lighthouse keeper."
    set_master_prompt_override(override)
    try:
        assert resolve_persona("dude", None) == override
        assert resolve_persona("pedro", None) == override
        assert resolve_persona("unknown", None) == override
    finally:
        set_master_prompt_override(None)
    assert resolve_persona("dude", None) == DEFAULT_DUDE_PROMPT


# ── the persona a chat really gets, for lines written outside the main reply ─

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_a_chats_own_persona_is_what_side_lines_are_written_in(monkeypatch):
    """A recalled picture's caption and a quiz verdict read the master prompt
    directly, so a chat on its own persona got them in Dale's voice."""
    from ipedro import personas

    monkeypatch.setattr(personas, "_master_prompt_override", None)
    chats = SimpleNamespace(get_config=AsyncMock(return_value=SimpleNamespace(
        persona="pirate", persona_custom="You talk like a pirate.")))
    assert await personas.persona_for_chat(chats, -5) == "You talk like a pirate."
    chats.get_config.return_value = SimpleNamespace(persona="neutral", persona_custom=None)
    assert await personas.persona_for_chat(chats, -5) == personas.NEUTRAL_PROMPT


@pytest.mark.asyncio
async def test_with_no_config_it_falls_back_to_the_master_prompt(monkeypatch):
    from ipedro import personas

    monkeypatch.setattr(personas, "_master_prompt_override", None)
    master = personas.current_master_prompt()
    assert await personas.persona_for_chat(None, -5) == master
    assert await personas.persona_for_chat(SimpleNamespace(
        get_config=AsyncMock(return_value=None)), -5) == master
    assert await personas.persona_for_chat(SimpleNamespace(
        get_config=AsyncMock(side_effect=RuntimeError("db"))), -5) == master
