"""Fit text into Telegram messages.

A message is capped at 4096 characters, and several admin commands print
lists whose length the operator doesn't control (a traceback in the log
ring buffer, a chat with a lot of facts, /activity 200). Each of them had
its own half-right splitter; two sent a chunk over the cap or an empty
first message. This is the one that doesn't.
"""

from __future__ import annotations

from collections.abc import Iterable

TELEGRAM_LIMIT = 4096
# Room under the cap for a "(2/5)" header line a caller may add.
DEFAULT_LIMIT = 3800


def _hard_split(line: str, limit: int) -> list[str]:
    """A line longer than `limit`, cut into pieces of at most `limit`,
    preferring to break at a space when there is one in the back half."""
    if len(line) <= limit:
        return [line]
    pieces: list[str] = []
    rest = line
    while len(rest) > limit:
        cut = rest.rfind(" ", limit // 2, limit + 1)
        cut = cut if cut > 0 else limit
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest)
    return pieces


def chunk_lines(lines: Iterable[str], limit: int = DEFAULT_LIMIT) -> list[str]:
    """Join `lines` with newlines into chunks of at most `limit` characters,
    never splitting a line unless it alone is longer than `limit`. No chunk
    is empty and no text is dropped."""
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        for piece in _hard_split(line, limit):
            add = len(piece) + (1 if buf else 0)
            if buf and size + add > limit:
                chunks.append("\n".join(buf))
                buf, size, add = [], 0, len(piece)
            buf.append(piece)
            size += add
    if buf:
        chunks.append("\n".join(buf))
    return [c.strip("\n") for c in chunks if c.strip("\n")]
