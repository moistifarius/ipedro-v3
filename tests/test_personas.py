from ipedro.personas import DEFAULT_PEDRO_PROMPT, PERSONAS, resolve_persona


def test_resolve_known_persona():
    assert resolve_persona("pedro", None) == DEFAULT_PEDRO_PROMPT
    assert resolve_persona("neutral", None) == PERSONAS["neutral"]


def test_resolve_unknown_falls_back_to_pedro():
    assert resolve_persona("does-not-exist", None) == DEFAULT_PEDRO_PROMPT


def test_custom_persona_overrides():
    custom = "You are a butler named Reginald."
    assert resolve_persona("pedro", custom) == custom


def test_pedro_master_prompt_override():
    from ipedro.personas import set_pedro_prompt_override
    override = "You are a stoic lighthouse keeper."
    set_pedro_prompt_override(override)
    try:
        assert resolve_persona("pedro", None) == override
        assert resolve_persona("unknown", None) == override
    finally:
        set_pedro_prompt_override(None)
    assert resolve_persona("pedro", None) == DEFAULT_PEDRO_PROMPT
