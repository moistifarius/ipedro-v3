"""Dale Gribble GIF library.

A small global table of King-of-the-Hill GIFs the bot sends as reactions.
Rows arrive two ways:

  * seeded from pinned URLs (``_SEED_GIFS``, applied by ``/dalegif seed``), and
  * captured from Telegram — an admin replies ``/dalegif <tags>`` to a GIF and
    we store Telegram's own ``file_id``.

``file_id`` is the good case: re-sending by file_id costs no upload at all.
A seeded url-only row therefore upgrades itself to a file_id the first time it
is sent successfully (see :func:`note_sent`), after which the library no longer
depends on the original host being alive.

Identity is ``file_unique_id``, not ``file_id`` — the latter is per-bot and can
be re-issued, the former is stable, so it's what answers "do we already have
this GIF?".

Plain module functions taking ``db``, the pattern used by reminders.py and
silenced_chats.py; the command router lives in handlers/dale.py.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx
from aiogram.types import BufferedInputFile, Message

from ipedro.bot_messages import track
from ipedro.db.pool import Database

log = logging.getLogger(__name__)

# Tags are single lowercase words so they're easy to type on a phone.
_TAG_RE = re.compile(r"[^a-z0-9]+")

_COLUMNS = "id, tags, file_id, file_unique_id, url, send_count"

# Seed downloads happen once per row (the send upgrades the row to a file_id),
# so unlike the automod media cache there is nothing worth keeping in memory.
_DOWNLOAD_TIMEOUT = 10.0
_MAX_BYTES = 10_000_000
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True)
class StoredGif:
    id: int
    tags: list[str]
    file_id: str | None
    file_unique_id: str | None
    url: str | None
    send_count: int


def normalize_tag(raw: str) -> str:
    """Lowercase, strip punctuation. '' when nothing usable is left."""
    return _TAG_RE.sub("", (raw or "").strip().lower())


def parse_tags(raw: str) -> list[str]:
    """Split a user-supplied tag string into normalized, de-duplicated tags."""
    seen: list[str] = []
    for part in (raw or "").split():
        tag = normalize_tag(part)
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def _row(record) -> StoredGif:
    data = dict(record)
    data["tags"] = list(data.get("tags") or [])
    return StoredGif(**data)


async def add(
    db: Database, tags: list[str], *, file_id: str | None = None,
    file_unique_id: str | None = None, url: str | None = None,
    added_by: int | None = None,
) -> tuple[int | None, bool]:
    """Store one GIF under ``tags``. Returns ``(id, was_new)``.

    A GIF we already hold is re-tagged rather than rejected — sending the same
    GIF again with different tags is the natural way to say "this one also
    works for X", so that's what it does.
    """
    tags = [t for t in (normalize_tag(t) for t in tags) if t]
    if not tags or not (file_id or url):
        return None, False

    if file_unique_id:
        row = await db.fetchrow(
            "INSERT INTO dale_gifs (tags, file_id, file_unique_id, url, added_by) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (file_unique_id) DO UPDATE "
            "   SET tags = ARRAY(SELECT DISTINCT unnest(dale_gifs.tags || $1)), "
            "       file_id = EXCLUDED.file_id "
            "RETURNING id, (xmax = 0) AS inserted",
            tags, file_id, file_unique_id, url, added_by,
        )
    else:
        row = await db.fetchrow(
            "INSERT INTO dale_gifs (tags, file_id, file_unique_id, url, added_by) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (url) DO NOTHING "
            "RETURNING id, TRUE AS inserted",
            tags, file_id, file_unique_id, url, added_by,
        )
    if not row:
        return None, False
    return int(row["id"]), bool(row["inserted"])


async def random_for_tag(db: Database, tag: str) -> StoredGif | None:
    """A random GIF for ``tag``; falls back to any GIF in the library.

    The fallback matters: a trigger should never silently do nothing just
    because that bucket hasn't been filled yet. A propane GIF answering
    "chemtrails" is still Dale, and still beats plain text.
    """
    tag = normalize_tag(tag)
    if tag:
        row = await db.fetchrow(
            f"SELECT {_COLUMNS} FROM dale_gifs WHERE $1 = ANY(tags) "
            " ORDER BY random() LIMIT 1",
            tag,
        )
        if row:
            return _row(row)
    row = await db.fetchrow(
        f"SELECT {_COLUMNS} FROM dale_gifs ORDER BY random() LIMIT 1"
    )
    return _row(row) if row else None


async def list_all(db: Database, tag: str | None = None) -> list[StoredGif]:
    tag = normalize_tag(tag or "")
    if tag:
        rows = await db.fetch(
            f"SELECT {_COLUMNS} FROM dale_gifs WHERE $1 = ANY(tags) ORDER BY id",
            tag,
        )
    else:
        rows = await db.fetch(f"SELECT {_COLUMNS} FROM dale_gifs ORDER BY id")
    return [_row(r) for r in rows]


async def remove(db: Database, gif_id: int) -> bool:
    status = await db.execute("DELETE FROM dale_gifs WHERE id = $1", gif_id)
    try:
        return int(str(status).split()[-1]) > 0
    except (ValueError, IndexError):
        return False


async def note_sent(
    db: Database, gif_id: int, file_id: str | None = None,
) -> None:
    """Record a successful send, learning the file_id when we just got one.

    Once a seeded url-row knows its file_id we drop the url: every later send
    is a bare string hand-off to Telegram with no download, and the row stops
    caring whether the original host is still up.
    """
    if file_id:
        await db.execute(
            "UPDATE dale_gifs SET send_count = send_count + 1, "
            "       file_id = $2, url = NULL "
            " WHERE id = $1",
            gif_id, file_id,
        )
    else:
        await db.execute(
            "UPDATE dale_gifs SET send_count = send_count + 1 WHERE id = $1",
            gif_id,
        )


# ── sending ──────────────────────────────────────────────────────────────────

async def _download(url: str) -> bytes | None:
    """Fetch seed bytes. None on any failure — the caller falls back to text.

    We fetch and upload the bytes ourselves rather than handing Telegram the
    URL: Telegram's own fetcher is far pickier than httpx with a browser UA,
    and this way a seeded URL only has to be reachable by us, once.
    """
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT,
            headers={"User-Agent": _UA},
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_BYTES:
                        log.info("dale gif too large: %s", url)
                        return None
                    chunks.append(chunk)
        return b"".join(chunks) or None
    except Exception as exc:
        log.info("dale gif fetch failed %s: %s", url, exc)
        return None


async def send_one(
    db: Database, msg: Message, gif: StoredGif, caption: str | None = None,
) -> Message | None:
    """Send one GIF as a reply. None if it couldn't be delivered."""
    payload: str | BufferedInputFile
    if gif.file_id:
        payload = gif.file_id
    elif gif.url:
        data = await _download(gif.url)
        if data is None:
            return None
        payload = BufferedInputFile(data, filename="dale.gif")
    else:
        return None

    try:
        sent = await msg.reply_animation(
            payload, caption=caption or None, disable_notification=True,
        )
    except Exception as exc:
        log.warning("dale gif send failed in %s: %s", msg.chat.id, exc)
        return None

    # Learn the file_id Telegram just handed back, so the next send is free.
    # Never let a bookkeeping failure undo a message the user already saw.
    try:
        learned = getattr(getattr(sent, "animation", None), "file_id", None)
        await note_sent(db, gif.id, learned if learned != gif.file_id else None)
    except Exception as exc:
        log.warning("dale gif bookkeeping failed for %s: %s", gif.id, exc)
    track(msg.chat.id, sent.message_id, caption or "[dale gif]")
    return sent


async def send_random(
    db: Database, msg: Message, tag: str, *,
    caption: str | None = None, fallback: str | None = None,
) -> bool:
    """Reply with a random Dale GIF for ``tag``. True when one went out.

    Falls back to ``fallback`` text on an empty library, a dead URL or a
    Telegram refusal, so a trigger never degrades into silence.
    """
    gif = await random_for_tag(db, tag)
    if gif is not None and await send_one(db, msg, gif, caption) is not None:
        return True
    if fallback:
        sent = await msg.reply(fallback, disable_notification=True)
        track(msg.chat.id, sent.message_id, fallback)
    return False
