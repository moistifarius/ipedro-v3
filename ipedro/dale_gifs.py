"""Dale Gribble GIF library.

A small global table of King-of-the-Hill GIFs the bot sends as reactions.
Rows arrive two ways:

  * seeded from pinned URLs (``dale_gif_seeds.SEED_GIFS``, applied by
    ``/dalegif seed``), and
  * captured from Telegram — an admin replies ``/dalegif <tags>`` to a GIF and
    we store Telegram's own ``file_id``.

``file_id`` is the good case: re-sending by file_id costs no upload at all.
A seeded url-only row therefore upgrades itself to a file_id the first time it
is sent successfully (see :func:`note_sent`), after which the library no longer
depends on the original host being alive.

Identity is ``file_unique_id``, not ``file_id`` — the latter is per-bot and can
be re-issued, the former is stable, so it's what answers "do we already have
this GIF?".

A GIF goes out only when it FITS: a tag with no GIF in it sends nothing (the
keyword bit says its text line instead) rather than some other clip, a chat
doesn't get the same clip twice in a row, and a keyword bit can't fire again
the moment after it did (see :func:`pace_ok`). Which GIF fits an unprompted
moment is ipedro/gif_reaction.py.

Plain module functions taking ``db``, the pattern used by reminders.py and
silenced_chats.py; the command router lives in handlers/dale.py.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

import httpx
from aiogram.types import BufferedInputFile, Message

from ipedro.bot_messages import track
from ipedro.dale_gif_seeds import SEED_GIFS
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


# ── pacing ───────────────────────────────────────────────────────────────────
# In memory, per chat, and reset by a restart (at worst one extra GIF).
_RECENT_PER_CHAT = 6            # GIFs remembered per chat, to not repeat them
TRIGGER_CHAT_GAP = 15.0         # s: no keyword bit right after any GIF here
TRIGGER_TAG_GAP = 90.0          # s: the same bit again in the same chat
_STATE_LIMIT = 500              # entries before stale ones are swept
FAIL_REST = 1800.0              # s a clip that couldn't be sent is left alone
_now = time.monotonic
_failing: dict[int, float] = {}                 # gif id -> not before this
_recent_ids: dict[int, deque[int]] = {}
_last_gif_at: dict[int, float] = {}
_last_tag_at: dict[tuple[int, str], float] = {}


def recent_ids(chat_id: int) -> list[int]:
    """The GIFs this chat saw lately, oldest first."""
    return list(_recent_ids.get(chat_id, ()))


def pace_ok(chat_id: int, tag: str) -> bool:
    """May a keyword bit for ``tag`` fire in this chat now? Not within
    TRIGGER_CHAT_GAP of any GIF here, nor TRIGGER_TAG_GAP of the same bit:
    "propane propane propane" is one joke, not three."""
    now = _now()
    if now - _last_gif_at.get(chat_id, -1e9) < TRIGGER_CHAT_GAP:
        return False
    return now - _last_tag_at.get((chat_id, normalize_tag(tag)), -1e9) >= TRIGGER_TAG_GAP


def note_gif(chat_id: int, tag: str = "", gif_id: int | None = None) -> None:
    """Remember that a GIF (or the text line standing in for one) just went
    out in this chat, for the pacing above and the no-repeat rule."""
    now = _now()
    _last_gif_at[chat_id] = now
    if tag:
        _last_tag_at[(chat_id, normalize_tag(tag))] = now
    if gif_id is not None:
        _recent_ids.setdefault(chat_id, deque(maxlen=_RECENT_PER_CHAT)).append(gif_id)
    if len(_last_tag_at) > _STATE_LIMIT or len(_last_gif_at) > _STATE_LIMIT:
        for key in [k for k, t in _last_tag_at.items() if now - t > 3600]:
            del _last_tag_at[key]
        for key in [k for k, t in _last_gif_at.items() if now - t > 3600]:
            del _last_gif_at[key]
            _recent_ids.pop(key, None)


def _note_failed(gif_id: int) -> None:
    """A clip that couldn't be sent: leave it be for a while. Bounded."""
    if len(_failing) > _STATE_LIMIT:
        now = _now()
        for key in [k for k, until in _failing.items() if until <= now]:
            del _failing[key]
    _failing[gif_id] = _now() + FAIL_REST


def forget_state() -> None:
    """Clear the pacing state (tests, and nothing else)."""
    _failing.clear()
    _recent_ids.clear()
    _last_gif_at.clear()
    _last_tag_at.clear()


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


async def random_for_tag(
    db: Database, tag: str, *, avoid: Sequence[int] = (),
    exclude_tags: frozenset[str] = frozenset(),
) -> StoredGif | None:
    """A GIF for ``tag``, or None when that bucket is empty.

    There is deliberately no "any GIF" fallback: a clip that doesn't fit what
    was just said (a propane GIF answering "chemtrails") is worse than the
    text line the caller falls back to. ``avoid`` is what this chat just saw,
    oldest first: those are passed over, the least-sent of the rest preferred;
    and when the chat has seen them all, the one seen longest ago comes round
    again. So a bucket of three is rotated, never rolled. A clip that couldn't
    be sent a moment ago (a dead link) is left alone for FAIL_REST, so it can't
    keep winning its tag; ``exclude_tags`` drops any clip carrying one of them.
    """
    tag = normalize_tag(tag)
    if not tag:
        return None
    rows = await db.fetch(
        f"SELECT {_COLUMNS} FROM dale_gifs WHERE $1 = ANY(tags) ORDER BY id",
        tag,
    )
    gifs = [g for g in map(_row, rows) if not set(g.tags) & exclude_tags]
    now = _now()
    gifs = [g for g in gifs if _failing.get(g.id, 0.0) <= now] or gifs
    if not gifs:
        return None
    seen = list(avoid)
    fresh = [g for g in gifs if g.id not in seen]
    if fresh:
        least = min(g.send_count for g in fresh)
        return random.choice([g for g in fresh if g.send_count <= least + 1])
    last_seen = {gif_id: place for place, gif_id in enumerate(seen)}   # latest wins
    return min(gifs, key=lambda g: last_seen[g.id])


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

    Once a seeded url-row knows its file_id every later send is a bare string
    hand-off to Telegram with no download, and the row stops caring whether
    the original host is still up. The url STAYS on the row, though: it is
    what `/dalegif seed` dedupes on (ON CONFLICT (url)). Clearing it, as
    this used to, made the next seed run insert a second copy of every GIF
    that had been sent once, and a third on the run after.
    """
    if file_id:
        await db.execute(
            "UPDATE dale_gifs SET send_count = send_count + 1, "
            "       file_id = $2 "
            " WHERE id = $1",
            gif_id, file_id,
        )
    else:
        await db.execute(
            "UPDATE dale_gifs SET send_count = send_count + 1 WHERE id = $1",
            gif_id,
        )


# ── seeding ──────────────────────────────────────────────────────────────────
# Pinned starter set: URLs verified by download + eye at the time they were
# added. Applied by `/dalegif seed` rather than at startup, deliberately — a
# startup seed would resurrect anything the user deleted on the next restart.
# Re-running is a no-op (url is UNIQUE), and each row stops depending on the
# host as soon as its first send upgrades it to a file_id.
#
# The table itself lives in dale_gif_seeds.py (data only — see there).
_SEED_GIFS = SEED_GIFS

# Hosts we're willing to pin. Mirrors the automod media test's allowlist.
# Enforced HERE at seed time, not only by a test: the seed table is plain
# content that can merge unreviewed (see merge_policy.py), and so can test
# files — a check that lived only in a test could be loosened in the same
# change that added the URL.
SEED_HOSTS = frozenset({"media.tenor.com", "media1.tenor.com",
                        "media.giphy.com", "i.imgflip.com"})


def _seed_host_ok(url: str) -> bool:
    parts = url.split("/")
    return url.startswith("https://") and len(parts) > 2 and parts[2] in SEED_HOSTS


async def apply_seed(db: Database) -> tuple[int, int, int]:
    """Insert the pinned starter set. Returns (added, already_present,
    refused) — refused being URLs on a host outside SEED_HOSTS."""
    added = skipped = refused = 0
    for url, tags in _SEED_GIFS:
        if not _seed_host_ok(url):
            log.warning("seed GIF refused, host not allowed: %s", url)
            refused += 1
            continue
        gif_id, was_new = await add(db, list(tags), url=url)
        if gif_id is not None and was_new:
            added += 1
        else:
            skipped += 1
    return added, skipped, refused


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
    *, tag: str = "",
) -> Message | None:
    """Send one GIF as a reply. None if it couldn't be delivered."""
    payload: str | BufferedInputFile
    if gif.file_id:
        payload = gif.file_id
    elif gif.url:
        data = await _download(gif.url)
        if data is None:
            _note_failed(gif.id)
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
        _note_failed(gif.id)
        return None
    _failing.pop(gif.id, None)

    # Learn the file_id Telegram just handed back, so the next send is free.
    # Never let a bookkeeping failure undo a message the user already saw.
    try:
        learned = getattr(getattr(sent, "animation", None), "file_id", None)
        await note_sent(db, gif.id, learned if learned != gif.file_id else None)
    except Exception as exc:
        log.warning("dale gif bookkeeping failed for %s: %s", gif.id, exc)
    note_gif(msg.chat.id, tag, gif.id)
    track(msg.chat.id, sent.message_id, caption or "[dale gif]")
    return sent


async def send_random(
    db: Database, msg: Message, tag: str, *,
    caption: str | None = None, fallback: str | None = None,
    pace: bool = False,
) -> bool:
    """Reply with a Dale GIF for ``tag``. True when one went out.

    Falls back to ``fallback`` text on an empty bucket, a dead URL or a
    Telegram refusal, so a trigger never degrades into silence. ``pace`` is for
    a keyword bit that fired on somebody's words: when the same bit (or any GIF)
    just went out in this chat it stays quiet, text line included, and either
    way a bit that fires counts as having just gone off.
    """
    chat_id = msg.chat.id
    if pace:
        if not pace_ok(chat_id, tag):
            return False
        note_gif(chat_id, tag)
    gif = await random_for_tag(db, tag, avoid=recent_ids(chat_id))
    if gif is not None and await send_one(db, msg, gif, caption, tag=tag) is not None:
        return True
    if fallback:
        sent = await msg.reply(fallback, disable_notification=True)
        track(msg.chat.id, sent.message_id, fallback)
    return False
