"""The auto-grudge: insults aimed at the bot, and only those.

The grudge makes the bot markedly snarkier toward someone for a day, so a
false positive is the bot turning on a person who said nothing to it. The
first detector joined any insult word to any name within 40 characters and
did exactly that to "my car is broken dude" and "the hinge is rusty and
broken" once the identity work added `rusty` and `dale` to the names.
"""

from __future__ import annotations

import pytest

from ipedro.identity import DALE
from ipedro.user_flags import is_insult_to_bot


@pytest.mark.parametrize("text", [
    "dale you're useless", "rusty is so dumb", "shut up rusty shackleford",
    "pedro is trash", "useless bot", "kys bot", "Dale Gribble is a piece of shit",
    "fuck you dale", "dale, you are such a dumb ass", "you stupid bot",
    "shut up you fucking bot", "bot is stupid", "dale shut up", "bot fuck off",
    "you are dumb dale", "stfu dale", "dude you're garbage", "duderino you're useless",
    "That's it, DALE IS TRASH",
])
def test_an_insult_aimed_at_him_registers(text):
    assert is_insult_to_bot(text), text


@pytest.mark.parametrize("text", [
    "i love dale", "that was stupid of me", "the dale cooper reveal",
    "my car is broken dude", "the hinge is rusty and broken",
    "rusty nails are garbage", "dale cooper is trash", "this movie is trash dude",
    "the bot is broken", "i hate the dude", "the new dale episode is garbage",
    "dude shut the door", "shut up about the bot", "bots are useless",
    "that's garbage dude", "this song is trash, duder", "fuck you dude",
    "", None,
])
def test_ordinary_speech_that_merely_contains_both_does_not(text):
    assert not is_insult_to_bot(text), text


def test_a_bug_report_is_not_an_insult():
    """'the bot is broken' is somebody telling the operator something."""
    assert not is_insult_to_bot("the bot is broken again, can someone look")


def test_it_follows_the_configured_names():
    from types import SimpleNamespace
    from ipedro import identity

    hank = identity.from_settings(SimpleNamespace(
        bot_name="Hank", bot_aliases="hank, hank hill", bot_flavor="plain"))
    assert is_insult_to_bot("hank hill you're useless", hank.names_pattern)
    assert is_insult_to_bot("useless bot", hank.names_pattern)
    assert not is_insult_to_bot("dale you're useless", hank.names_pattern)
    # a bot that really is called Dude can be insulted by adjacency
    dude = identity.from_settings(SimpleNamespace(
        bot_name="Dude", bot_aliases="dude", bot_flavor="plain"))
    assert is_insult_to_bot("useless dude", dude.names_pattern)


def test_the_default_names_are_dales():
    assert is_insult_to_bot("rusty you're useless", DALE.names_pattern)
