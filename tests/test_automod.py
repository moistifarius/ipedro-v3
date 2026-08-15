"""AutoModerator-style canned-response triggers (real r/shitposting bits)."""

from __future__ import annotations

import random
import re

from ipedro.handlers.chat import (
    _ALL_YOUR_BASE, _AMONG_US_COPYPASTA, _AUTOMOD_TRIGGERS, _COPIUM_LINES,
    _GAY_COPYPASTA, _GNU_LINUX_PASTA, _HOLY_HELL_CHAIN, _KYS_LINES,
    _L_RATIO_COPYPASTA, _RIZZ_LINES, _automod_response,
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


def test_kys_gets_a_deflection_and_wins_priority():
    for t in ("kys", "kill yourself", "just neck yourself", "i want to kill myself",
              "killurself", "kill ur self"):
        assert _automod_response(t, random.Random(0)) in _KYS_LINES, t
    # kys intercepts first — a joke trigger in the same message can't win
    assert _automod_response("kys you gay loser", random.Random(0)) in _KYS_LINES
    assert _automod_response("kys you gay loser") != _GAY_COPYPASTA
    # deflection register only — never an actual instruction to self-harm.
    # Word-boundaried so "skill issue" (contains 'kill') is allowed.
    banned = re.compile(r"\b(kill|kys|die|neck|rope)\b", re.IGNORECASE)
    for line in _KYS_LINES:
        assert not banned.search(line), line
    # not a false positive on unrelated 'kill' phrasing
    assert _automod_response("that joke killed me lol") not in _KYS_LINES


def test_holy_hell_summons_the_google_en_passant_chain():
    assert _automod_response("holy hell") == _HOLY_HELL_CHAIN
    assert _automod_response("google en passant").startswith("Holy hell!")
    assert "Google en passant" in _automod_response("HOLY HELL what is that")
    # needs the whole phrase, not a bare 'hell'
    assert _automod_response("what the hell") != _HOLY_HELL_CHAIN


def test_l_ratio_fires_only_on_the_taunt_forms():
    assert _automod_response("L + ratio") == _L_RATIO_COPYPASTA
    assert _automod_response("l+ratio bozo") == _L_RATIO_COPYPASTA
    assert _automod_response("you just got ratioed").startswith("don't care")
    assert _automod_response("get ratio'd") == _L_RATIO_COPYPASTA
    # bare 'ratio' is far too common (aspect ratio, gear ratio, math) to fire
    assert _automod_response("what's the aspect ratio") is None
    assert _automod_response("a 16:9 ratio") is None
    assert _automod_response("the gear ratios spin") is None
    # a stray 'l' before an unrelated 'ratio' must not trip it either
    assert _automod_response("cool aspect ratio") is None


def test_nl_never_lucky_is_a_standalone_token():
    assert _automod_response("nl") == "Never lucky."
    assert _automod_response("man, nl") == "Never lucky."
    # must be its own word, not a substring of ordinary English
    assert _automod_response("only kidding") is None
    assert _automod_response("channel surfing") is None


def test_copium_draws_from_the_tuple_branch():
    # Every draw is a real member of the tuple...
    for seed in range(30):
        out = _automod_response("massive copium", random.Random(seed))
        assert out in _COPIUM_LINES
    # ...and it genuinely varies (exercises r.choice, not a fixed return).
    seen = {_automod_response("copium", random.Random(s)) for s in range(30)}
    assert len(seen) > 1
    # boundary: not a substring match
    assert _automod_response("copiumm") is None


def test_scraped_copypastas_fire_verbatim():
    assert _automod_response("all your base are belong to us") == _ALL_YOUR_BASE
    assert "For great justice." in _automod_response("ALL YOUR BASE")
    assert _automod_response("i use linux btw") == _GNU_LINUX_PASTA
    assert _automod_response("linux").startswith("I'd just like to interject")


def test_brainrot_reactions_and_their_boundaries():
    assert _automod_response("it's morbin time") == "It's Morbin' Time."
    assert _automod_response("morbius sweep") == "It's Morbin' Time."
    assert _automod_response("skibidi toilet") == "skibidi bop bop yes yes 🚽"
    assert _automod_response("that's so ohio") == "only in Ohio 💀"
    assert _automod_response("what the sigma") == "what the sigma?"
    assert _automod_response("bro has mad rizz", random.Random(0)) in _RIZZ_LINES
    assert _automod_response("we live in a society").startswith("🃏")
    # bare 'society' is too common to fire the full-phrase trigger
    assert _automod_response("in modern society") is None
    # single-word triggers stay word-boundaried
    assert _automod_response("the stigma around it") is None
    assert _automod_response("morbid curiosity") is None
    assert _automod_response("ohioan pride") is None
    assert _automod_response("rizzling up") is None


def test_every_response_fits_the_telegram_message_limit():
    # Telegram hard-caps a text message at 4096 chars; keep headroom.
    for _pattern, response in _AUTOMOD_TRIGGERS:
        variants = response if isinstance(response, tuple) else (response,)
        for v in variants:
            assert isinstance(v, str) and 0 < len(v) <= 4000, v[:60]


def test_no_trigger_returns_none():
    assert _automod_response("just a normal message") is None
    assert _automod_response("") is None
    assert _automod_response(None) is None


def test_table_shape_is_extensible():
    for pattern, response in _AUTOMOD_TRIGGERS:
        assert hasattr(pattern, "search")
        assert isinstance(response, (str, tuple))
