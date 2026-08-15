"""AutoModerator-style canned-response triggers (real r/shitposting bits)."""

from __future__ import annotations

from ipedro.handlers.chat import (
    _AMONG_US_COPYPASTA, _AUTOMOD_TRIGGERS, _GAY_COPYPASTA, _KYS_RESPONSE,
    _automod_response,
)


def test_gay_returns_the_copypasta():
    assert _automod_response("that's so gay lol") == _GAY_COPYPASTA
    assert _GAY_COPYPASTA.startswith("Yeah can you imagine being gay lol?")


def test_gay_matches_standalone_word_only():
    for t in ("gay", "you're GAY", "so gay!", "gays", "a gay man"):
        assert _automod_response(t) == _GAY_COPYPASTA, t
    for t in ("gaymer", "gaylord", "gayer", "gayest", "margay"):
        assert _automod_response(t) != _GAY_COPYPASTA, t


def test_among_us():
    assert _automod_response("have you played among us").startswith("Stop posting about Among Us!")
    assert _automod_response("amogus").startswith("Stop posting about")
    assert _automod_response("that's kinda sussy") == _AMONG_US_COPYPASTA
    # bare 'sus' is too common to fire the wall of text
    assert _automod_response("that seems sus") is None
    assert _automod_response("suspicious activity") is None


def test_based_sneed_trans():
    assert _automod_response("this is so based") == "Based? Based on what?"
    assert _automod_response("based on the data") == "Based? Based on what?"
    assert _automod_response("sneed") == "Formerly Chuck's."
    assert _automod_response("trans rights now") == "🏳️‍⚧️ trans rights are human rights."


def test_number_jokes_need_boundaries():
    assert _automod_response("nice 69 haha") == "nice"
    assert _automod_response("420 baby") == "blaze it 🔥"
    assert _automod_response("born in 1969") is None
    assert _automod_response("that costs 4200") is None


def test_kys_gets_the_sincere_response_and_wins_priority():
    for t in ("kys", "kill yourself", "just neck yourself", "i want to kill myself",
              "killurself", "kill ur self"):
        assert _automod_response(t) == _KYS_RESPONSE, t
    # sincere response takes priority even alongside a joke trigger
    assert _automod_response("kys you gay loser") == _KYS_RESPONSE
    # not a false positive on unrelated 'kill' phrasing
    assert _automod_response("that joke killed me lol") != _KYS_RESPONSE


def test_no_trigger_returns_none():
    assert _automod_response("just a normal message") is None
    assert _automod_response("") is None
    assert _automod_response(None) is None


def test_table_shape_is_extensible():
    for pattern, response in _AUTOMOD_TRIGGERS:
        assert hasattr(pattern, "search")
        assert isinstance(response, (str, tuple))
