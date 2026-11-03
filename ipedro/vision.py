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
import re
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
    size: int | None = None     # bytes, as Telegram reports them

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
            size=getattr(a, "file_size", None),
        )

    if msg.video:
        v = msg.video
        return Media(
            kind="video", file_unique_id=v.file_unique_id,
            label=f"a video{_clock(v.duration)}", file_id=_thumb_id(v),
            source_file_id=v.file_id, seconds=getattr(v, "duration", None),
            size=getattr(v, "file_size", None),
        )

    if msg.video_note:
        v = msg.video_note
        return Media(
            kind="video note", file_unique_id=v.file_unique_id,
            label=f"a video note{_clock(v.duration)}", file_id=_thumb_id(v),
            source_file_id=v.file_id, seconds=getattr(v, "duration", None),
            size=getattr(v, "file_size", None),
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
                source_file_id=d.file_id, size=getattr(d, "file_size", None),
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


def note(media: Media, description: str | None, *, glimpse: bool = False) -> str:
    """The bracketed line that stands in for the media in the transcript.

    Bracketed and self-contained so it reads unambiguously next to a
    caption, and so it still makes sense months later when the summarizer
    or a semantic hit pulls it back out of history. A video that could only
    be glimpsed (one frame: too big, too long, over a limit) says so, so a
    one-picture description is never taken for a watched video.
    """
    if description:
        kind = f"{media.kind}, one frame only" if glimpse else media.kind
        return f"[{kind}: {description}]"
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

# What a speech-to-text model writes for silence, music and noise: a sign-off,
# a plea to subscribe, "Amara.org", a sound in brackets. (Whisper does it
# constantly.) Kept out of the description, where "the best line" would be
# promoted to something the video said.
_NOT_SPEECH = re.compile(
    r"^(?:(?:thank you|thanks)(?: so much| very much)?"
    r"(?: for (?:watching|listening|your time|tuning in)"
    r"(?: and (?:ill|i will) see you (?:in the |at the )?next (?:video|one|time))?)?"
    r"|(?:please )?(?:like and )?subscribe(?: to (?:my|the|our) channel)?"
    r"|bye(?: bye)?|goodbye|you|music|applause|laughter|silence"
    r"|(?:subtitles?|subtitled|captions?|captioned|transcription|transcribed"
    r"|translation|translated)(?: \w+){0,2} by\b.*|.*amara org.*)$"
)
_SOUND_NOTE = re.compile(r"\[[^\]]{0,40}\]|\([^)]{0,40}\)|[♪♫🎵🎶]+")


def usable_transcript(text: str | None) -> str | None:
    """`text` if it reads as something said, None if it is the filler a
    transcriber invents (see _NOT_SPEECH), a loop of one line, or nothing."""
    sentences = re.split(r"(?<=[.!?…])\s+|\n+", _SOUND_NOTE.sub(" ", (text or "").strip()))
    norm = [" ".join(re.findall(r"\w+", sentence.lower().replace("'", ""))) for sentence in sentences]
    norm = [n for n in norm if n]
    if not norm or all(_NOT_SPEECH.match(n) for n in norm):
        return None
    if len(norm) >= 4 and len(set(norm)) * 3 <= len(norm):       # a loop
        return None
    return (text or "").strip()


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


def _take_slots(rt, user_id: int | None, chat_id: int | None):
    """Spend this look's hourly slots; returns None when over a limit, else a
    function that hands them back (for a look that never got to cost anything).
    Someone with no id (an anonymous admin, a channel post) is limited as a
    person per chat, not as one person for the whole bot."""
    if user_id in getattr(rt.settings, "admin_ids", ()):
        return lambda: None
    person = user_id if user_id is not None else -(chat_id or 0)
    keys = [("video", "user", person)]
    if chat_id is not None:
        keys.append(("video", "chat", chat_id))
    limits = [ratelimit.LIMITS["video"], ratelimit.CHAT_LIMITS["video"]]
    taken = []
    for key, (uses, window) in zip(keys, limits):
        if ratelimit.LIMITER.check(key, limit=uses, window=window):
            for k in taken:
                ratelimit.LIMITER.refund(k)
            return None
        taken.append(key)

    def give_back() -> None:
        for k in taken:
            ratelimit.LIMITER.refund(k)
    return give_back


# One look per file at a time: a video posted twice, or an album, or the same
# file in two chats at once, is watched once and shared, not paid for each.
_inflight: dict[str, "asyncio.Future[str | None]"] = {}


async def _watch_media(
    rt, msg: Message, media: Media, *, transcribe: bool, paid: bool = True,
    typing: bool = True,
) -> str | None:
    """A description of the whole video, or None to fall back to the
    thumbnail. Never raises. ``paid=False`` only reads what is already
    cached: the replied-to path must not start a watch charged to whoever
    posted the video."""
    keys = ("v2:",) if transcribe else ("v2n:", "v2:")
    for prefix in keys:
        cached = await _cached(rt.db, prefix + media.file_unique_id)
        if cached:
            return cached
    if not paid:
        return None
    max_seconds = getattr(rt.settings, "video_vision_max_seconds", 300)
    # Turned away before it costs anyone a slot: too long or too big to be
    # watched at all (most phone videos are over 20 MB).
    if media.seconds and media.seconds > max_seconds:
        return None
    if media.size and media.size > _MAX_VIDEO_BYTES:
        return None
    key = ("v2:" if transcribe else "v2n:") + media.file_unique_id
    running = _inflight.get(key)
    if running is not None:
        return await asyncio.shield(running)
    work = asyncio.ensure_future(
        _watch_once(rt, msg, media, transcribe, max_seconds, typing, key))
    _inflight[key] = work
    work.add_done_callback(lambda _f: _inflight.pop(key, None))
    return await asyncio.shield(work)


async def _watch_once(
    rt, msg: Message, media: Media, transcribe: bool, max_seconds: int,
    typing: bool, key: str,
) -> str | None:
    chat_id = msg.chat.id if msg.chat else None
    user_id = msg.from_user.id if msg.from_user else None
    # A person posting a stream of videos shouldn't be able to run up a bill
    # of frames and transcripts; past the limit it's the thumbnail again.
    give_back = _take_slots(rt, user_id, chat_id)
    if give_back is None:
        log.info("Not watching a %s: rate limited.", media.kind)
        return None
    spent = False
    try:
        pulse = _typing(rt.bot, chat_id) if typing else contextlib.nullcontext()
        async with pulse:
            data = await _download_video(rt.bot, media.source_file_id)
            if not data:
                return None
            parts = await video_frames.extract(
                data, max_frames=rt.settings.video_vision_frames,
                want_audio=transcribe, max_seconds=max_seconds,
            )
            if parts is None:
                return None
            spent = True                 # ffmpeg ran: from here it cost something
            transcript, transcribed = None, True
            if transcribe and parts.audio:
                raw = await rt.openai.transcribe(
                    io.BytesIO(parts.audio), filename="video.mp3", chat_id=chat_id,
                    seconds=min(parts.seconds, video_frames.AUDIO_SECONDS),
                )
                transcribed = raw is not None     # None is a failure, not silence
                transcript = usable_transcript(raw)
            speech = (
                VIDEO_SPEECH_PRESENT.format(
                    transcript=transcript[:_MAX_TRANSCRIPT_CHARS])
                if transcript else VIDEO_SPEECH_ABSENT
            )
            n = len(parts.frames)
            seconds = (
                f"{parts.seconds:.0f}" if parts.seconds >= 10
                else f"{parts.seconds:.1f}"
            )
            description = await rt.openai.describe_images(
                parts.frames, media_type="image/jpeg", max_tokens=400,
                prompt=VIDEO_DESCRIBE_PROMPT.format(
                    frames=f"{n} frame" + ("" if n == 1 else "s"),
                    seconds=seconds, speech=speech,
                ),
                chat_id=chat_id,
            )
    except Exception as exc:
        log.warning("Watching a %s failed: %s", media.kind, exc)
        return None
    finally:
        if not spent:
            give_back()
    if description:
        # A transcription that FAILED is not "no speech": kept as frames-only,
        # so a chat that wants the sound watches it again, instead of the gap
        # being cached as the answer.
        await _remember(
            rt.db, media, description,
            key=key if transcribed else "v2n:" + media.file_unique_id,
        )
    return description or None


async def look(
    rt, msg: Message, *, transcribe: bool = False, watch: bool = True,
    typing: bool = True,
) -> Seen | None:
    """Everything the bot can say about the media on ``msg``, or None if
    there is none. ``transcribe`` is whether the chat allows the sound of a
    video to be turned into text (its voice-transcription setting). ``watch``
    is whether a video may be watched anew (False: only what's cached, else
    the thumbnail). ``typing`` is whether to show "typing…" while a slow look
    runs (only worth it when he is about to answer).

    Never raises: a blind spot is a missing sentence, not a dropped
    message. Every failure path falls back to the plain label so the bot
    still knows a photo arrived, just not what was in it.
    """
    media = extract_media(msg)
    if media is None:
        return None
    description = None
    deep = media.watchable and watching(rt)
    if deep:
        description = await _watch_media(
            rt, msg, media, transcribe=transcribe, paid=watch, typing=typing)
    glimpse = deep and description is None
    if description is None and media.viewable:
        description = await _describe_media(rt, msg, media)
    return Seen(
        media=media, description=description,
        note=note(media, description, glimpse=glimpse),
    )


async def describe(
    rt, msg: Message, *, transcribe: bool = False, watch: bool = True,
) -> str | None:
    """The transcript line for whatever media is on ``msg``, or None."""
    seen = await look(rt, msg, transcribe=transcribe, watch=watch, typing=False)
    return seen.note if seen else None
