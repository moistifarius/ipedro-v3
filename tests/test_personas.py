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
