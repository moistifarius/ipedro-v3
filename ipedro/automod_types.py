"""The two automod replies that aren't plain text.

Kept apart from the rule table (ipedro/handlers/automod_bits.py) on purpose.
That table is plain content an /evolve change may merge without the owner
looking, which is only safe while it is data. These record types are the only
code it is allowed to name, and they hold no behaviour: a change to them
waits for the owner like any other code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple


class MediaResponse(NamedTuple):
    """An automod reply that is an actual meme image ('photo') or GIF ('gif')."""

    kind: str          # "photo" | "gif"
    url: str           # pinned direct media URL (https)
    caption: str       # sent with the media; also the tracked snippet
    fallback: str      # text reply used when fetching/sending the media fails


@dataclass(frozen=True)
class DaleGif:
    """Reply with a random Dale Gribble GIF drawn from the library.

    A marker only: this module stays pure (no Runtime, no I/O), so it just
    names a tag and chat.py resolves it against the DB at send time.

    Deliberately a dataclass and NOT a NamedTuple. A NamedTuple *is* a tuple,
    so it would fall into `_automod_response`'s "tuple means pick one at
    random" branch and return one of these three fields as the reply — and
    `test_table_shape_is_extensible`, which checks `isinstance(r, tuple)`,
    would happily pass while it happened.
    """

    tag: str
    caption: str = ""
    fallback: str = ""
