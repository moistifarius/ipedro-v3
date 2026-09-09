"""The pictures a chat has posted, kept so the bot can bring one back.

Vision already turns every photo into a sentence in the transcript, so the
bot *remembers* pictures the way it remembers anything said. What that
alone can't do is hand the picture back: "dale send that pic of the
grill" needs the Telegram file id, who posted it and when, tied to the
description — and a way to search that by meaning.

So every described picture also gets a row here, per chat, with its file
id, and the description (plus caption, poster and date) is embedded under
ref_kind 'media' in the same embeddings table the rest of memory uses.
Recall is one vector search away; without pgvector it degrades to a
keyword match on the description.

The library is chat memory like everything else: it respects the memory
switch, it is wiped with the conversation, and it follows the chat
through a supergroup migration (it has a chat_id column).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from aiogram.types import Message

from ipedro.bot_messages import track
from ipedro.memory.context_builder import _humanize_span
from ipedro.personas import current_master_prompt
from ipedro.vision import Media

log = logging.getLogger(__name__)

# Below this the "best" match is a guess; the bot says so instead.
_MIN_SIMILARITY = 0.32

_SEND_RE = re.compile(
    r"\b(?:send|show|post|find|pull up|dig up|repost|resend|re-send|share|"
    r"drop|gimme|give me|get me|bring back|link|where(?:'s| is| was))\b",
    re.IGNORECASE,
)
_MEDIA_RE = re.compile(
    r"\b(?:pics?|pictures?|photos?|images?|screenshots?|screencaps?|gifs?|"
    r"stickers?|memes?|videos?|clips?|selfies?)\b",
    re.IGNORECASE,
)
# Not a request for one of OURS: making a new one is the meme/image path.
_MAKE_RE = re.compile(r"\b(?:make|create|draw|generate|render)\b", re.IGNORECASE)


def detect_recall(text: str | None) -> bool:
    """'send that pic of the grill', 'where's the photo matt posted',
    'show me the screenshot again' — a request for a picture we may have."""
    text = (text or "").strip()
    if not text or _MAKE_RE.search(text):
        return False
    return bool(_SEND_RE.search(text) and _MEDIA_RE.search(text))


def _searchable(description: str, *, caption: str | None,
                posted_by_name: str | None, when: datetime | None) -> str:
    """What gets embedded: the picture plus the who/when people actually
    ask by ('the one matt posted', 'from last week')."""
    bits = [description]
    if caption:
        bits.append(f"caption: {caption}")
    if posted_by_name:
        bits.append(f"posted by {posted_by_name}")
    if when:
        bits.append(when.strftime("on %A %-d %B %Y"))
    return ". ".join(bits)


async def remember(
    rt, *, chat_id: int, message_id: int | None, media: Media,
    description: str, caption: str | None, posted_by: int | None,
    posted_by_name: str | None,
) -> int | None:
    """Store one described picture for this chat. Idempotent per message.
    Returns the row id, or None when the row already existed / on failure."""
    try:
        row_id = await rt.db.fetchval(
            "INSERT INTO media_library (chat_id, message_id, file_unique_id, "
            " file_id, kind, description, caption, posted_by, posted_by_name) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
            "ON CONFLICT (chat_id, message_id) DO NOTHING RETURNING id",
            chat_id, message_id, media.file_unique_id, media.file_id,
            media.kind, description, caption or None, posted_by, posted_by_name,
        )
    except Exception as exc:
        log.warning("media library insert failed in %s: %s", chat_id, exc)
        return None
    if row_id is None:
        return None
    store = rt.memory
    if store.openai and store.pgvector_available:
        text = _searchable(description, caption=caption,
                           posted_by_name=posted_by_name,
                           when=datetime.now(timezone.utc))
        embedding = await store.openai.embed(text)
        if embedding:
            await store.embeddings.upsert(chat_id, "media", row_id, text, embedding)
    return row_id


async def _rows_by_id(db, chat_id: int, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    rows = await db.fetch(
        "SELECT id, file_id, kind, description, caption, posted_by_name, "
        "       created_at FROM media_library WHERE chat_id = $1 AND id = ANY($2)",
        chat_id, ids,
    )
    return {r["id"]: dict(r) for r in rows}


async def search(rt, chat_id: int, query: str, *, k: int = 3) -> list[dict]:
    """Best-first candidates for ``query`` among this chat's pictures. Each
    carries a ``similarity`` (vector) or None (keyword fallback)."""
    store = rt.memory
    if store.openai and store.pgvector_available and query.strip():
        embedding = await store.openai.embed(query)
        if embedding:
            hits = await store.embeddings.search(chat_id, embedding, k=k * 4)
            media_hits = [h for h in hits if h.get("ref_kind") == "media"][:k]
            rows = await _rows_by_id(rt.db, chat_id, [int(h["ref_id"]) for h in media_hits])
            out = []
            for h in media_hits:
                row = rows.get(int(h["ref_id"]))
                if row:
                    out.append({**row, "similarity": float(h.get("similarity") or 0)})
            if out:
                return out
    # Keyword fallback: any distinctive word in the description or caption.
    words = [w for w in re.findall(r"[a-z0-9']+", query.lower()) if len(w) >= 4]
    if not words:
        return []
    pattern = "%(" + "|".join(re.escape(w) for w in words) + ")%"
    rows = await rt.db.fetch(
        "SELECT id, file_id, kind, description, caption, posted_by_name, created_at "
        "  FROM media_library "
        " WHERE chat_id = $1 AND (description ILIKE $2 OR caption ILIKE $2) "
        " ORDER BY created_at DESC LIMIT $3",
        chat_id, pattern, k,
    )
    return [{**dict(r), "similarity": None} for r in rows]


async def recent(rt, chat_id: int, *, limit: int = 8) -> list[dict]:
    rows = await rt.db.fetch(
        "SELECT id, kind, description, posted_by_name, created_at "
        "  FROM media_library WHERE chat_id = $1 "
        " ORDER BY created_at DESC LIMIT $2",
        chat_id, limit,
    )
    return [dict(r) for r in rows]


def _ago(when: datetime | None) -> str:
    if not when:
        return "a while back"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return _humanize_span((datetime.now(timezone.utc) - when).total_seconds(),
                          suffix=" ago")


async def _caption(rt, row: dict, chat_id: int) -> str:
    """One in-character line to go with the picture; a plain fallback if
    the model is out."""
    who = row.get("posted_by_name") or "somebody"
    plain = f"{who}, {_ago(row.get('created_at'))}."
    try:
        line = await rt.openai.cheap_chat(
            [
                {"role": "system", "content": current_master_prompt()},
                {"role": "user", "content": (
                    f"You are sending back a {row.get('kind', 'picture')} "
                    f"that {who} posted in this chat {_ago(row.get('created_at'))}. "
                    f"It shows: {row.get('description', '')[:300]}\n\n"
                    "Write ONE short line to go with it, in character, "
                    "under 20 words. Output only the line."
                )},
            ],
            max_tokens=60, chat_id=chat_id,
        )
    except Exception as exc:                      # pragma: no cover - defensive
        log.info("recall caption failed in %s: %s", chat_id, exc)
        line = None
    return (line or "").strip() or plain


async def send(rt, msg: Message, row: dict, caption: str | None) -> Message | None:
    """Reply with the stored file, by kind. None if Telegram refused it."""
    kind, file_id = row.get("kind"), row["file_id"]
    try:
        if kind == "photo":
            return await msg.reply_photo(file_id, caption=caption)
        if kind == "gif":
            return await msg.reply_animation(file_id, caption=caption)
        if kind == "video":
            return await msg.reply_video(file_id, caption=caption)
        if kind == "video note":
            return await msg.reply_video_note(file_id)
        if kind == "sticker":
            return await msg.reply_sticker(file_id)
        return await msg.reply_document(file_id, caption=caption)
    except Exception as exc:
        log.warning("re-sending stored %s failed: %s", kind, exc)
        return None


async def recall(rt, msg: Message, cfg, query: str) -> bool:
    """Find the picture they mean and send it back. True when one went out.

    A miss returns False and sends nothing; the caller lets the normal
    reply say so in the bot's own voice rather than a canned line.
    """
    chat_id = msg.chat.id
    candidates = await search(rt, chat_id, query, k=3)
    best = candidates[0] if candidates else None
    if best is None or (
        best["similarity"] is not None and best["similarity"] < _MIN_SIMILARITY
    ):
        return False
    caption = await _caption(rt, best, chat_id)
    sent = await send(rt, msg, best, caption if best.get("kind") != "sticker" else None)
    if sent is None:
        return False
    track(chat_id, sent.message_id, caption)
    if cfg.memory_enabled:
        # So he knows he sent it, and can talk about it afterwards.
        try:
            await rt.memory.record_message(
                chat_id=chat_id, role="assistant",
                content=f"[sent back a saved {best.get('kind', 'picture')}: "
                        f"{best.get('description', '')[:200]}] {caption}",
                message_id=sent.message_id, user_id=None, do_embed=False,
            )
        except Exception as exc:
            log.warning("recall record failed in %s: %s", chat_id, exc)
    return True
