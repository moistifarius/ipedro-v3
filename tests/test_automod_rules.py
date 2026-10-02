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
from ipedro.handlers.automod import (
    _AUTOMOD_TRIGGERS, _KYS_LINES, MediaResponse, _automod_response,
)


# ── the kys deflection ───────────────────────────────────────────────────────

def test_kys_gets_a_deflection_and_wins_priority():
    for t in ("kys", "kill yourself", "just neck yourself",
              "i want to kill myself", "killurself", "kill ur self"):
        assert _automod_response(t, random.Random(0)) in _KYS_LINES, t
    # kys intercepts first — a joke trigger in the same message can't win
    assert _automod_response("kys you gay loser", random.Random(0)) in _KYS_LINES
    # deflection register only — never an actual instruction to self-harm.
    banned = re.compile(r"\b(kill|kys|die|neck|rope)\b", re.IGNORECASE)
    for line in _KYS_LINES:
        assert not banned.search(line), line
    assert _automod_response("that joke killed me lol") not in _KYS_LINES


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
