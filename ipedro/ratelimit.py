"""A small per-person brake on the commands that cost real money.

/aigen is about $0.04 an image, /ether is text-to-speech plus a voice note
dropped into a different chat, and /a goes to the main model. None had any
limit, so anyone the bot could hear (including a stranger in a DM, since a
private chat answers everyone) could run up the bill or spam another chat as
fast as Telegram would deliver the messages. Bot admins are exempt; nothing
else is.

In memory on purpose: it resets on restart, which at worst hands someone one
extra window. No database, nothing to migrate.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Hashable

# kind -> (uses, per this many seconds). Per person, per hour; /ether also
# has a per-chat budget below so a group can't be used to launder the limit
# through several accounts.
LIMITS: dict[str, tuple[int, float]] = {
    "image": (5, 3600.0),        # /aigen, /generate, /meme, "make a meme about X"
    "ether": (3, 3600.0),        # TTS + a voice note into another chat
    "ask": (30, 3600.0),         # /a: the main model
    "translate": (10, 3600.0),   # /aitranslate: audio transcription
    "duck": (6, 3600.0),         # /duckhunt: summoning a duck on demand. Each
                                 # one is a model call and a point to be had, so
                                 # a summon-bang loop was a free score farm.
}
CHAT_LIMITS: dict[str, tuple[int, float]] = {
    "ether": (10, 3600.0),
}


class Limiter:
    """Sliding-window counter. check() records a use when it allows one."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[Hashable, deque[float]] = {}
        self._calls = 0

    def check(self, key: Hashable, *, limit: int, window: float) -> float:
        """0.0 when allowed (and the use is recorded); otherwise the number
        of seconds until a slot frees up."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= window:
            hits.popleft()
        self._calls += 1
        if self._calls % 500 == 0:
            self._forget_idle(now, window)
        if len(hits) >= limit:
            return max(0.0, window - (now - hits[0]))
        hits.append(now)
        return 0.0

    def _forget_idle(self, now: float, window: float) -> None:
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] >= window]:
            del self._hits[key]

    def reset(self) -> None:
        self._hits.clear()


LIMITER = Limiter()


def wait_for(kind: str, user_id: int, chat_id: int | None = None) -> float:
    """Seconds this person must wait before using `kind` again, 0.0 if they
    may go now (in which case the use is counted). A refused use is not
    counted against the chat or person."""
    uses, window = LIMITS[kind]
    user_wait = LIMITER.check((kind, "user", user_id), limit=uses, window=window)
    if user_wait:
        return user_wait
    if chat_id is not None and kind in CHAT_LIMITS:
        cuses, cwindow = CHAT_LIMITS[kind]
        chat_wait = LIMITER.check((kind, "chat", chat_id), limit=cuses, window=cwindow)
        if chat_wait:
            # Their personal slot was already taken above; give it back so a
            # chat-wide refusal doesn't also burn the individual's budget.
            hits = LIMITER._hits.get((kind, "user", user_id))
            if hits:
                hits.pop()
            return chat_wait
    return 0.0


def describe_wait(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute" + ("" if minutes == 1 else "s")
