from ipedro.personas import PEDRO_PROMPT, PERSONAS, resolve_persona


def test_resolve_known_persona():
    assert resolve_persona("pedro", None) == PEDRO_PROMPT
    assert resolve_persona("neutral", None) == PERSONAS["neutral"]


def test_resolve_unknown_falls_back_to_pedro():
    assert resolve_persona("does-not-exist", None) == PEDRO_PROMPT


def test_custom_persona_overrides():
    custom = "You are a butler named Reginald."
    assert resolve_persona("pedro", custom) == custom
