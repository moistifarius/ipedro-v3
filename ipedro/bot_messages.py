"""Bounded ring buffer of the last N bot messages per chat.

Used by the admin's ``/delete_msg`` flow so the admin can scroll a recent
list and tap one to delete it. In-memory only — restarts wipe the buffer,
and that's fine because Telegram only allows the bot to delete its own
messages within a 48-hour window anyway.

Silencing a chat (admin override) lives in ``ipedro.silenced_chats``;
this module only cares about tracking what the bot sent.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass



@dataclass
class TrackedMessage:
    message_id: int
    sent_at: float
    snippet: str


# chat_id → bounded deque of the most recent N bot sends. 20 entries / chat
# is enough for the admin to scroll through; older entries silently drop.
_BUFFER_SIZE = 20
_recent_sends: dict[int, deque[TrackedMessage]] = {}


def track(chat_id: int, message_id: int | None, text: str | None) -> None:
    """Record that the bot just sent `message_id` to `chat_id`.

    Tolerant: silently no-ops if `message_id` is None (e.g. the send
    failed) or if `text` is None (e.g. it was a photo with no caption).
    The deque is created lazily on first use.
    """
    if message_id is None:
        return
    snippet = (text or "")[:60].replace("\n", " ")
    buf = _recent_sends.get(chat_id)
    if buf is None:
        buf = deque(maxlen=_BUFFER_SIZE)
        _recent_sends[chat_id] = buf
    buf.append(TrackedMessage(
        message_id=int(message_id),
        sent_at=time.time(),
        snippet=snippet,
    ))


def recent(chat_id: int) -> list[TrackedMessage]:
    """Return the tracked sends for `chat_id`, newest-first.

    Empty list if nothing has been tracked yet. The list is a snapshot —
    safe to mutate, doesn't affect the buffer."""
    buf = _recent_sends.get(chat_id)
    if not buf:
        return []
    return list(reversed(list(buf)))


def forget(chat_id: int, message_id: int) -> bool:
    """Drop `message_id` from the buffer for `chat_id` (after delete).

    Returns True if it was present and removed, False if not found.
    """
    buf = _recent_sends.get(chat_id)
    if not buf:
        return False
    for i, entry in enumerate(buf):
        if entry.message_id == message_id:
            del buf[i]
            return True
    return False


def reset_buffer(chat_id: int | None = None) -> None:
    """Test helper: clear the tracked-sends buffer."""
    if chat_id is None:
        _recent_sends.clear()
    else:
        _recent_sends.pop(chat_id, None)
