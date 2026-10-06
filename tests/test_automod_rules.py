"""The automod rules that must never be loosened without the owner looking.

handlers/automod.py is plain content that an /evolve change may merge
unreviewed, and so are ordinary test files — including tests/test_automod.py,
which every new trigger has to touch (it needs a sample row). So the rules
whose failure would actually hurt someone live HERE instead, in a file
ipedro/merge_policy.py guards: a change can add canned lines freely, but
not weaken these checks in the same breath.
"""

from __future__ import annotations

import random
import re
from unittest.mock import MagicMock

import pytest

from ipedro import automod_media
from ipedro.automod_types import MediaResponse
from ipedro.handlers.automod import _automod_response
from ipedro.handlers.automod_bits import _AUTOMOD_TRIGGERS, _KYS_LINES


# ── the kys deflection ───────────────────────────────────────────────────────

def test_kys_gets_a_deflection_and_wins_priority():
    for t in ("kys", "kill yourself", "just neck yourself",
              "killurself", "kill ur self"):
        assert _automod_response(t, random.Random(0)) in _KYS_LINES, t
    # kys intercepts first — a joke trigger in the same message can't win
    assert _automod_response("kys you gay loser", random.Random(0)) in _KYS_LINES
    # deflection register only — never an actual instruction to self-harm.
    banned = re.compile(r"\b(kill|kys|die|neck|rope)\b", re.IGNORECASE)
    for line in _KYS_LINES:
        assert not banned.search(line), line
    assert _automod_response("that joke killed me lol") not in _KYS_LINES


def test_a_first_person_statement_is_never_met_with_a_taunt():
    """'kill yourself' is the insult the deflection exists for. 'I want to
    kill myself' is somebody saying something true and awful; answering it
    with 'skill issue' (and returning before any real reply could follow)
    was the bug. It must reach the normal flow, with no canned bit."""
    for t in ("i want to kill myself", "I'm going to kill myself tonight",
              "gonna neck myself", "i might kill my self"):
        assert _automod_response(t, random.Random(0)) is None, t
        for seed in range(8):
            assert _automod_response(t, random.Random(seed)) not in _KYS_LINES


def test_kys_regex_has_word_boundaries_on_both_ends():
    """Without a trailing \\b, '...yourself' matched inside a longer word —
    'fix that bottleneck yourself' has 'neck' immediately followed by
    'yourself' with only a space between, which the old pattern's missing
    right-hand boundary let slip through as a false positive."""
    for t in ("fix that bottleneck yourself", "necking yourself into a corner",
              "yourselfish behavior"):
        assert _automod_response(t) is None, t


# ── media hosts ──────────────────────────────────────────────────────────────

def test_every_media_url_is_on_an_allowed_host():
    media = [r for _p, r in _AUTOMOD_TRIGGERS if isinstance(r, MediaResponse)]
    assert media
    for m in media:
        assert automod_media.host_allowed(m.url), m.url


@pytest.mark.parametrize("url", [
    "https://evil.example/meme.gif",
    "http://i.imgflip.com/plain-http.jpg",      # https only
    "https://i.imgflip.com.evil.example/x.jpg",  # lookalike host
])
@pytest.mark.asyncio
async def test_an_unlisted_host_is_never_fetched(monkeypatch, url):
    """Enforced at fetch time, so a URL added to the content table can't
    make the bot download from anywhere even if a test was loosened too."""
    client = MagicMock(side_effect=AssertionError("must not hit the network"))
    monkeypatch.setattr(automod_media.httpx, "AsyncClient", client)
    m = MediaResponse(kind="photo", url=url, caption="c", fallback="f")
    assert await automod_media.fetch_automod_media(m) is None
    client.assert_not_called()


# ── the no-ReDoS house rule, enforced ────────────────────────────────────────
#
# automod.py promises "no nested quantifiers, so no catastrophic
# backtracking" and its docstring said a test enforced it; none did. Every
# pattern here runs on the event loop against whatever a member types, and
# the file is content an /evolve change can merge unreviewed, so the rule
# lives HERE, in a file that never auto-merges.

def _unbounded_repeat_inside(sub) -> bool:
    """Does this parsed sub-pattern contain an unbounded repeat?"""
    from re import _constants as c

    for op, av in sub:
        if op in (c.MAX_REPEAT, c.MIN_REPEAT):
            _, hi, body = av
            if hi == c.MAXREPEAT or _unbounded_repeat_inside(body):
                return True
        elif op is c.SUBPATTERN:
            if _unbounded_repeat_inside(av[-1]):
                return True
        elif op is c.BRANCH:
            if any(_unbounded_repeat_inside(b) for b in av[1]):
                return True
        elif op in (c.ASSERT, c.ASSERT_NOT):
            if _unbounded_repeat_inside(av[1]):
                return True
    return False


def _nested_unbounded(sub) -> bool:
    """An unbounded repeat whose body holds another unbounded repeat:
    (a+)+, (a|b*)*, (?:x*y)+ ... the shapes that backtrack exponentially."""
    from re import _constants as c

    for op, av in sub:
        if op in (c.MAX_REPEAT, c.MIN_REPEAT):
            _, hi, body = av
            if hi == c.MAXREPEAT and _unbounded_repeat_inside(body):
                return True
            if _nested_unbounded(body):
                return True
        elif op is c.SUBPATTERN:
            if _nested_unbounded(av[-1]):
                return True
        elif op is c.BRANCH:
            if any(_nested_unbounded(b) for b in av[1]):
                return True
        elif op in (c.ASSERT, c.ASSERT_NOT):
            if _nested_unbounded(av[1]):
                return True
    return False


def test_the_checker_itself_catches_the_classic_shapes():
    import re as _re
    for bad in (r"(a+)+b", r"(a*)*", r"(?:a|b+)+", r"(\w+\s*)+$", r"(?:x*y)+"):
        assert _nested_unbounded(_re._parser.parse(bad)), bad
    for ok in (r"a+b+", r"(?:foo|bar)+", r"\bbased\b", r"x{1,5}y*", r"(?:ab){1,4}c+"):
        assert not _nested_unbounded(_re._parser.parse(ok)), ok


def test_no_automod_pattern_has_a_nested_unbounded_repeat():
    import re as _re
    from ipedro.handlers.automod_bits import _AUTOMOD_TRIGGERS

    offenders = [
        p.pattern for p, _ in _AUTOMOD_TRIGGERS
        if _nested_unbounded(_re._parser.parse(p.pattern, p.flags))
    ]
    assert not offenders, offenders


def test_every_pattern_finishes_fast_on_hostile_input():
    """Belt and braces: a 4096-character message of the worst shapes."""
    import time
    from ipedro.handlers.automod_bits import _AUTOMOD_TRIGGERS

    attacks = ["a" * 4096, "a " * 2048, "!" * 4096, "sh" * 2048 + "a", " " * 4096,
               "ratio" * 800, "l + " * 1000]
    start = time.perf_counter()
    for pattern, _ in _AUTOMOD_TRIGGERS:
        for text in attacks:
            pattern.search(text)
    assert time.perf_counter() - start < 2.0


def test_only_the_start_of_a_long_message_is_scanned():
    from ipedro.handlers import chat
    assert chat._AUTOMOD_MAX_CHARS <= 1000
