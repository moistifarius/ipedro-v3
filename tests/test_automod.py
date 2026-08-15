"""AutoModerator-style canned-response triggers (r/shitposting vibe)."""

from __future__ import annotations

from ipedro.handlers.chat import _AUTOMOD_TRIGGERS, _GAY_COPYPASTA, _automod_response


def test_gay_returns_the_copypasta():
    assert _automod_response("that's so gay lol") == _GAY_COPYPASTA
    assert _GAY_COPYPASTA.startswith("Yeah can you imagine being gay lol?")
    assert _GAY_COPYPASTA.endswith("Couldn’t be me lmao.")


def test_gay_matches_standalone_word_only():
    for t in ("gay", "you're GAY", "so gay!", "gays", "a gay man"):
        assert _automod_response(t) == _GAY_COPYPASTA, t
    for t in ("gaymer", "gaylord", "gayer", "gayest", "margay", "okay"):
        # None of these are the gay bit (may still be None or another trigger).
        assert _automod_response(t) != _GAY_COPYPASTA, t


def test_seeded_bits():
    assert _automod_response("this is based") == "Based on what?"
    assert _automod_response("haha 69 nice") == "nice"
    assert _automod_response("420 baby") == "blaze it 🔥"
    assert _automod_response("trans rights are human rights") == "🏳️‍⚧️ trans rights"


def test_numbers_need_word_boundaries():
    assert _automod_response("born in 1969") is None       # 69 inside a year
    assert _automod_response("that costs 4200") is None     # 420 inside a number
    assert _automod_response("69") == "nice"


def test_no_trigger_returns_none():
    assert _automod_response("just a normal message") is None
    assert _automod_response("") is None
    assert _automod_response(None) is None


def test_first_match_wins():
    # 'gay' precedes 'based' in the table, so a message with both is the bit.
    assert _automod_response("gay and based") == _GAY_COPYPASTA


def test_table_shape_is_extensible():
    # Each row is (compiled-regex, str | tuple[str, ...]) — the extension point.
    for pattern, response in _AUTOMOD_TRIGGERS:
        assert hasattr(pattern, "search")
        assert isinstance(response, (str, tuple))
