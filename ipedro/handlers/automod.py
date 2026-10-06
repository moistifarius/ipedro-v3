"""Picks the AutoModerator-style canned reply for a message, if any.

The bits themselves — the copypastas, the meme media, and the table that maps
a pattern to a response — are plain data in ipedro/handlers/automod_bits.py,
so an /evolve change may add one without the owner looking. This file is the
part that DECIDES, so it isn't content: first match wins, a tuple means pick
one at random, and the Dale GIF rows are skipped for a bot that isn't Dale.
It is consulted before the normal AI reply.
"""

from __future__ import annotations

import random

from ipedro.automod_types import DaleGif, MediaResponse
from ipedro.handlers.automod_bits import _AUTOMOD_TRIGGERS

__all__ = ["DaleGif", "MediaResponse", "_AUTOMOD_TRIGGERS", "_automod_response"]


def _automod_response(
    text: str | None, rng: random.Random | None = None, *,
    dale_gifs: bool = True,
) -> "str | MediaResponse | DaleGif | None":
    """First matching AutoMod-style canned response for `text`, or None.

    ``dale_gifs=False`` passes over the Dale GIF rows — a bot that isn't
    Dale has no GIFs of itself to send — and keeps scanning, so a later
    row that also matches still gets its turn."""
    if not text:
        return None
    r = rng or random
    for pattern, response in _AUTOMOD_TRIGGERS:
        if not dale_gifs and isinstance(response, DaleGif):
            continue
        if pattern.search(text):
            # Both markers pass through untouched; only a real tuple means
            # "pick one of these at random".
            if isinstance(response, (MediaResponse, DaleGif)):
                return response
            return response if isinstance(response, str) else r.choice(response)
    return None
