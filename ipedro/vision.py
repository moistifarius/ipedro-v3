"""Let the bot see: turn a photo, sticker, GIF or video into words.

Telegram hands us binary media; the model reads text. So every visual
message gets converted to a short written description at arrival, and
from there it flows through the exact same pipeline as a typed message —
recorded in memory, summarized, embedded, recalled months later. That's
the whole design: describe once, then it's just text.

The same shape as voice transcription (`_transcribe_voice` in
handlers/chat.py), for the same reason.

Three tiers, by how much we can actually see:

  * a still image we can look at (photo, static sticker, or the thumbnail
    Telegram generates for a GIF/video)  → a real description;
  * a video, GIF or video note (small enough to download, short enough to
    be worth it) → a handful of frames sampled across it, plus a transcript
    of what was said where the chat has voice transcription on, described
    together. Anything that stops that (no ffmpeg, too big, too long, a rate
    limit, a file that isn't really a video) falls back to the thumbnail;
  * media with no still frame (animated stickers are Lottie JSON, voice
    notes are audio)                     → a label, so the bot at least
    knows something arrived and what it was;
  * nothing recognizable                 → None, and the caller carries on.

Descriptions are cached globally by ``file_unique_id`` — Telegram's stable
per-file identity. Stickers and forwarded memes repeat constantly, so the
cache is most of the cost control: the tenth time someone sends the same
reaction sticker it costs nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from dataclasses import dataclass

from aiogram.types import Message

from ipedro import ratelimit, video_frames
from ipedro.prompts import (
    VIDEO_DESCRIBE_PROMPT, VIDEO_SPEECH_ABSENT, VIDEO_SPEECH_PRESENT,
)

log = logging.getLogger(__name__)

# Formats every vision provider accepts. Sniffed from the bytes rather
# than trusted from a file extension — Telegram serves sticker thumbnails
# with cheerfully inaccurate names.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# Base64 inflates by 4/3 on the way to the provider, and Anthropic caps a
# single image at 5MB *encoded* — so the raw ceiling is 3.5MB, not 5. Well
# clear of any real Telegram photo; refusing early beats a provider 413.
_MAX_IMAGE_BYTES = 3_500_000

DESCRIBE_PROMPT = (
    "Describe this image for someone who can't see it. One to three "
    "sentences, concrete and specific: who or what is in it, what's "
    "happening, where, and the mood of it. If there's any text in the "
    "image, quote it exactly — on a meme or a screenshot the text IS the "
    "point, so never skip it. Name the person, character, show, or meme "
    "format if you recognize it. Write only the description: no preamble, "
    "no 'this image shows', no commentary about the image being an image."
)


def sniff_image_type(data: bytes) -> str | None:
    """The image's real media type, or None if it isn't a still image we
    can send to a vision model (an animated .tgs sticker, say)."""
    for magic, media_type in _MAGIC:
        if data.startswith(magic):
            return media_type
    # WEBP is RIFF-framed: "RIFF" <4 byte length> "WEBP".
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@dataclass(frozen=True)
class Media:
    """One piece of media on a message, and how to look at it."""

    kind: str                   # photo | sticker | gif | video | video note | …
    file_unique_id: str
    label: str                  # what to say when we can't see it
    file_id: str | None = None  # the still frame to look at, when there is one
    # The file itself, as Telegram can send it back. NOT file_id: for a GIF,
    # video, video note, animated sticker or non-image file, file_id is the
    # THUMBNAIL's id, which Telegram will only send as a photo. The picture
    # library files this one, so "send that gif" can actually send the gif.
    source_file_id: str | None = None
    seconds: int | None = None  # as Telegram reports it, for video-like media

    @property
    def viewable(self) -> bool:
        return self.file_id is not None

    @property
    def watchable(self) -> bool:
        """Is this something to sample frames from (the file itself, not
        its thumbnail)?"""
        return (
            self.kind in ("video", "gif", "video note")
            and self.source_file_id is not None
        )

    @property
    def sendable_id(self) -> str | None:
        """What to store so the media can be sent back as itself."""
        return self.source_file_id or self.file_id


def _clock(seconds: int | None) -> str:
    if not seconds:
        return ""
    return f", {seconds // 60}:{seconds % 60:02d}"


def _thumb_id(obj) -> str | None:
    thumb = getattr(obj, "thumbnail", None)
    return thumb.file_id if thumb else None


def extract_media(msg: Message) -> Media | None:
    """The one piece of media worth describing on this message, if any.

    Telegram never puts two of these on one message, so first match wins.
    """
    if msg.photo:
        # PhotoSize list runs smallest → largest. Detail matters — a
        # downscaled meme loses the text that was the whole point — so take
        # the biggest one that still fits the provider's ceiling rather than
        # the biggest one outright. An unknown file_size is assumed fine;
        # the download re-checks against the real size anyway.
        best = next(
            (p for p in reversed(msg.photo)
             if (p.file_size or 0) <= _MAX_IMAGE_BYTES),
            msg.photo[0],
        )
        return Media(
            kind="photo", file_unique_id=best.file_unique_id,
            label="a photo", file_id=best.file_id, source_file_id=best.file_id,
        )

    if msg.sticker:
        s = msg.sticker
        bits = [b for b in (s.emoji, f'from "{s.set_name}"' if s.set_name else "")
                if b]
        label = "a sticker" + (f" ({' '.join(bits)})" if bits else "")
        # A static sticker IS a WEBP image. Animated (.tgs, Lottie JSON) and
        # video (.webm) stickers aren't, so we look at the still thumbnail
        # Telegram generates for them instead.
        still = s.file_id if not (s.is_animated or s.is_video) else _thumb_id(s)
        return Media(
            kind="sticker", file_unique_id=s.file_unique_id,
            label=label, file_id=still, source_file_id=s.file_id,
        )

    if msg.animation:                       # a GIF, which Telegram stores as mp4
        a = msg.animation
        return Media(
            kind="gif", file_unique_id=a.file_unique_id,
            label="a GIF", file_id=_thumb_id(a), source_file_id=a.file_id,
            seconds=getattr(a, "duration", None),
        )

    if msg.video:
        v = msg.video
        return Media(
            kind="video", file_unique_id=v.file_unique_id,
            label=f"a video{_clock(v.duration)}", file_id=_thumb_id(v),
            source_file_id=v.file_id, seconds=getattr(v, "duration", None),
        )

    if msg.video_note:
        v = msg.video_note
        return Media(
            kind="video note", file_unique_id=v.file_unique_id,
            label=f"a video note{_clock(v.duration)}", file_id=_thumb_id(v),
            source_file_id=v.file_id, seconds=getattr(v, "duration", None),
        )

    if msg.document:
        d = msg.document
        mime = (d.mime_type or "").lower()
        name = d.file_name or "a file"
        # An image sent as a file (uncompressed) is still an image.
        is_image = mime.startswith("image/")
        still = d.file_id if is_image else _thumb_id(d)
        if mime.startswith("video/"):               # a video sent as a file
            return Media(
                kind="video", file_unique_id=d.file_unique_id,
                label=f"a video ({name})", file_id=still,
                source_file_id=d.file_id,
            )
        return Media(
            kind="image" if is_image else "file",
            file_unique_id=d.file_unique_id,
            label=f"a file ({name})", file_id=still,
            source_file_id=d.file_id,
        )

    if msg.audio:
        a = msg.audio
        title = " ".join(p for p in (a.performer, a.title) if p)
        return Media(
            kind="audio", file_unique_id=a.file_unique_id,
            label=f"an audio file{f' ({title})' if title else ''}"
                  f"{_clock(a.duration)}",
        )

    return None


def note(media: Media, description: str | None) -> str:
    """The bracketed line that stands in for the media in the transcript.

    Bracketed and self-contained so it reads unambiguously next to a
    caption, and so it still makes sense months later when the summarizer
    or a semantic hit pulls it back out of history.
    """
    if description:
        return f"[{media.kind}: {description}]"
    return f"[{media.label}]"


async def _cached(db, file_unique_id: str) -> str | None:
    try:
        row = await db.fetchrow(
            "SELECT description FROM media_descriptions "
            "WHERE file_unique_id = $1",
            file_unique_id,
        )
    except Exception as exc:                      # pragma: no cover - defensive
        log.warning("media cache read failed: %s", exc)
        return None
    return row["description"] if row else None


async def _remember(db, media: Media, description: str, *, key: str | None = None) -> None:
    try:
        await db.execute(
            "INSERT INTO media_descriptions (file_unique_id, kind, description) "
            "VALUES ($1, $2, $3) ON CONFLICT (file_unique_id) DO NOTHING",
            key or media.file_unique_id, media.kind, description,
        )
    except Exception as exc:                      # pragma: no cover - defensive
        log.warning("media cache write failed: %s", exc)


async def _download(bot, file_id: str) -> bytes | None:
    file = await bot.get_file(file_id)
    if (file.file_size or 0) > _MAX_IMAGE_BYTES:
        log.info("Skipping oversized media: %s bytes", file.file_size)
        return None
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    return buf.getvalue()


@dataclass(frozen=True)
class Seen:
    """What the bot made of one message's media."""

    media: Media
    description: str | None     # None when we could only label it
    note: str                   # the transcript line


async def _describe_media(rt, msg: Message, media: Media) -> str | None:
    cached = await _cached(rt.db, media.file_unique_id)
    if cached:
        return cached
    try:
        data = await _download(rt.bot, media.file_id)
        if not data:
            return None
        media_type = sniff_image_type(data)
        if media_type is None:
            log.info("Media %s isn't a still image; labeling only.", media.kind)
            return None
        description = await rt.openai.describe_image(
            data, media_type=media_type, prompt=DESCRIBE_PROMPT,
            chat_id=msg.chat.id if msg.chat else None,
        )
    except Exception as exc:
        log.warning("Vision failed on %s: %s", media.kind, exc)
        return None
    if description:
        await _remember(rt.db, media, description)
    return description or None


# ── watching a video ─────────────────────────────────────────────────────────
#
# A deep look is cached under its own key, so the one-frame description an
# older version stored for the same file doesn't shadow it, and one made
# without the sound ("v2n:") doesn't pretend to include it ("v2:").
_MAX_VIDEO_BYTES = 20_000_000     # what Telegram lets a bot download at all
_MAX_TRANSCRIPT_CHARS = 2500


def watching(rt) -> bool:
    """Is this deployment set up to watch videos (frames wanted, ffmpeg here)?"""
    return (
        getattr(rt.settings, "video_vision_frames", 0) >= 1
        and video_frames.available()
    )


@contextlib.asynccontextmanager
async def _typing(bot, chat_id: int | None):
    """Keep "typing…" showing while a slow look is under way; a video takes
    seconds and the chat should not look dead."""
    async def pulse() -> None:
        while True:
            await asyncio.sleep(4)
            try:
                await bot.send_chat_action(chat_id, "typing")
            except Exception:                    # cosmetic; never fail a look
                pass

    if chat_id is not None:
        try:                                     # shown at once, then kept up
            await bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
    task = asyncio.create_task(pulse()) if chat_id is not None else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()


async def _download_video(bot, file_id: str) -> bytes | None:
    file = await bot.get_file(file_id)
    if (file.file_size or 0) > _MAX_VIDEO_BYTES:
        return None
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    return buf.getvalue()


async def _watch_media(
    rt, msg: Message, media: Media, *, transcribe: bool,
) -> str | None:
    """A description of the whole video, or None to fall back to the
    thumbnail. Never raises."""
    keys = ("v2:",) if transcribe else ("v2n:", "v2:")
    for prefix in keys:
        cached = await _cached(rt.db, prefix + media.file_unique_id)
        if cached:
            return cached
    max_seconds = getattr(rt.settings, "video_vision_max_seconds", 300)
    if media.seconds and media.seconds > max_seconds:
        return None
    chat_id = msg.chat.id if msg.chat else None
    user_id = msg.from_user.id if msg.from_user else None
    # A person posting a stream of videos shouldn't be able to run up a bill
    # of frames and transcripts; past the limit it's the thumbnail again.
    if user_id not in getattr(rt.settings, "admin_ids", ()):
        wait = ratelimit.wait_for("video", user_id or 0, chat_id)
        if wait:
            log.info("Not watching a %s: rate limited for %.0fs.", media.kind, wait)
            return None
    try:
        async with _typing(rt.bot, chat_id):
            data = await _download_video(rt.bot, media.source_file_id)
            if not data:
                return None
            parts = await video_frames.extract(
                data, max_frames=rt.settings.video_vision_frames,
                want_audio=transcribe, max_seconds=max_seconds,
            )
            if parts is None:
                return None
            transcript = None
            if transcribe and parts.audio:
                transcript = await rt.openai.transcribe(
                    io.BytesIO(parts.audio), filename="video.mp3", chat_id=chat_id,
                )
            speech = (
                VIDEO_SPEECH_PRESENT.format(
                    transcript=transcript[:_MAX_TRANSCRIPT_CHARS])
                if transcript else VIDEO_SPEECH_ABSENT
            )
            description = await rt.openai.describe_images(
                parts.frames, media_type="image/jpeg", max_tokens=400,
                prompt=VIDEO_DESCRIBE_PROMPT.format(
                    frames=len(parts.frames), seconds=round(parts.seconds),
                    speech=speech,
                ),
                chat_id=chat_id,
            )
    except Exception as exc:
        log.warning("Watching a %s failed: %s", media.kind, exc)
        return None
    if description:
        await _remember(
            rt.db, media, description,
            key=("v2:" if transcribe else "v2n:") + media.file_unique_id,
        )
    return description or None


async def look(rt, msg: Message, *, transcribe: bool = False) -> Seen | None:
    """Everything the bot can say about the media on ``msg``, or None if
    there is none. ``transcribe`` is whether the chat allows the sound of a
    video to be turned into text (its voice-transcription setting).

    Never raises: a blind spot is a missing sentence, not a dropped
    message. Every failure path falls back to the plain label so the bot
    still knows a photo arrived, just not what was in it.
    """
    media = extract_media(msg)
    if media is None:
        return None
    description = None
    if media.watchable and watching(rt):
        description = await _watch_media(rt, msg, media, transcribe=transcribe)
    if description is None and media.viewable:
        description = await _describe_media(rt, msg, media)
    return Seen(media=media, description=description, note=note(media, description))


async def describe(rt, msg: Message, *, transcribe: bool = False) -> str | None:
    """The transcript line for whatever media is on ``msg``, or None."""
    seen = await look(rt, msg, transcribe=transcribe)
    return seen.note if seen else None
