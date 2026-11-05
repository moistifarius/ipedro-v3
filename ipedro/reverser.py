"""The bot plays a video backwards.

``/reverse`` on a video, GIF or round video (replied to, or sent with
``/reverse`` as its caption), or in plain words at the start of a message to a
bot that is answering anyway: "dale reverse this", "play this backwards",
"rewind that gif", "reverse this and mute it". The result comes back as a reply
of the same kind (a video, a looping GIF, a round video). The work is in
ipedro/video_reverse.py; this is the asking, the limits and the sending.

No model is called, so there is no money in it, only CPU: so reversals are rate
limited (kind ``reverse``), share ffmpeg's two slots with watching videos, and
whatever can be refused from what Telegram says of the file (too long, too big)
is refused before anything is downloaded.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import re
from dataclasses import dataclass

from aiogram.types import BufferedInputFile, Message, ReplyParameters

from ipedro import poll_maker, ratelimit, video_reverse, vision
from ipedro.bot_messages import track
from ipedro.handlers.common import over_limit
from ipedro.identity import DALE
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

_SCAN_CHARS = 300

NO_VIDEO = (
    "Reply to a video, a GIF or a round video with /reverse, or send one with "
    "/reverse as its caption."
)
TOO_BIG = "That file is over 20 MB, which is as much as Telegram lets me take."
DOWNLOAD_FAILED = "Couldn't get that video from Telegram. Try again."
NOT_VIDEO = "I couldn't open that as a video."
BIG_PICTURE = "That video's picture is too big for me to open."
BUSY = "Busy with other videos right now. Try again in a minute."
UNAVAILABLE = "I can't reverse videos here (there's no ffmpeg)."
FAILED = "Couldn't reverse that one."
TOO_LARGE_OUTPUT = "It came out too big to send. Try a shorter clip."


def too_long(seconds: float) -> str:
    return (f"That one runs {seconds:.0f} seconds. I can reverse up to "
            f"{video_reverse.MAX_SECONDS}.")


# ── recognising the ask ─────────────────────────────────────────────────────

# What can follow the verb: a pointer at the video, politeness, and the words
# that say to leave the sound out. Anything else ("reverse psychology", "reverse
# the order of the list") means it wasn't an ask.
_VOCAB = frozenset((
    "this that it the a an my his her their its video vid gif clip animation "
    "note circle round one above below here there please pls thanks thx ty and "
    "mute muted silent silently without with sound audio no backwards backward "
    "reverse reversed"
).split())
_MUTE = re.compile(
    r"\b(?:mute|muted|silent|silently)\b|\b(?:without|no)\s+(?:the\s+)?(?:sound|audio)\b",
    re.IGNORECASE,
)
_ASK = (
    r"(?P<verb>reverse|rewind)\b"
    r"|(?:play|run|do|show|make|put)\s+(?:this|that|it|the\s+(?:video|vid|gif|clip|animation))"
    r"(?:\s+(?:video|vid|gif|clip|animation))?\s+(?:(?:to\s+)?(?:play|go|run)\s+)?"
    r"(?:in\s+)?(?:backwards?|reverse)\b"
)


@dataclass(frozen=True)
class ReverseAsk:
    mute: bool = False
    # "reverse"/"rewind" is about a video however it is worded. "do it
    # backwards" may be about anything, so without a video in sight it is left
    # to the ordinary reply instead of being answered "reply to a video".
    clear: bool = False


@functools.lru_cache(maxsize=32)
def _ask_re(names: str) -> re.Pattern:
    return re.compile(
        rf"^\W*{poll_maker.lead_in(names)}(?P<ask>{_ASK})", re.IGNORECASE,
    )


def detect_reverse_request(text: str | None, *, names: str | None = None) -> ReverseAsk | None:
    """None when `text` isn't asking for a video to be reversed. The ask must
    open the message, and what follows it may only point at the video (so
    "reverse psychology" and "reverse the list" are not asks)."""
    if not text or len(text) > _SCAN_CHARS:    # a long message isn't "reverse this"
        return None
    names = names or DALE.names_pattern
    m = _ask_re(names).match(text)
    if m is None:
        return None
    rest = text[m.end():]
    bare = poll_maker.polite_re(names).sub(" ", rest)
    if any(t not in _VOCAB for t in re.findall(r"[\w']+", bare.lower())):
        return None
    return ReverseAsk(mute=bool(_MUTE.search(rest)), clear=m.group("verb") is not None)


# ── what there is to reverse ────────────────────────────────────────────────

def has_own_video(msg: Message) -> bool:
    """Does this message itself carry a video, GIF or round video?"""
    media = vision.extract_media(msg)
    return media is not None and media.watchable


def find_video(msg: Message) -> tuple[vision.Media, Message] | None:
    """The video, GIF or round video this message has, else the one it replies
    to, with the message that holds it."""
    for holder in (msg, getattr(msg, "reply_to_message", None)):
        if holder is None:
            continue
        media = vision.extract_media(holder)
        if media is not None and media.watchable:
            return media, holder
    return None


@contextlib.asynccontextmanager
async def _acting(bot, chat_id: int, action: str):
    """Keep "sending a video…" showing while the job runs: it takes seconds."""
    async def pulse() -> None:
        while True:
            await asyncio.sleep(4)
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id, action)

    with contextlib.suppress(Exception):
        await bot.send_chat_action(chat_id, action)
    task = asyncio.create_task(pulse())
    try:
        yield
    finally:
        task.cancel()


async def _say(msg: Message, text: str) -> str:
    with contextlib.suppress(Exception):
        await msg.reply(text, disable_notification=True)
    return text


def _refund(rt: Runtime, msg: Message) -> None:
    """Give back the use that was counted: nothing was done (a refusal before
    any ffmpeg ran)."""
    user = getattr(msg, "from_user", None)
    if user is None or user.id in rt.settings.admin_ids:
        return
    ratelimit.LIMITER.refund(("reverse", "user", user.id))
    ratelimit.LIMITER.refund(("reverse", "chat", msg.chat.id))


_REFUSALS = {
    "unavailable": UNAVAILABLE, "not-video": NOT_VIDEO, "too-big": TOO_BIG,
    "big-picture": BIG_PICTURE, "busy": BUSY, "failed": FAILED,
    "too-large-output": TOO_LARGE_OUTPUT,
}
# Given back: nothing was downloaded or run (he was busy, or has no ffmpeg).
# NOT given back: a refusal after the download (a wrong file, a picture too big),
# which cost a download and a probe, or someone could make him fetch 20 MB files
# at will without using up their hour.
_NOTHING_DONE = {"unavailable", "busy"}


# ── the whole job ───────────────────────────────────────────────────────────

async def _send(rt: Runtime, msg: Message, kind: str, made: video_reverse.Reversed) -> object:
    """Send the result as what it was: a video, a looping GIF, a round video."""
    thread = (
        getattr(msg, "message_thread_id", None)
        if getattr(msg, "is_topic_message", False) else None
    )
    common = dict(
        chat_id=msg.chat.id, message_thread_id=thread, disable_notification=True,
        reply_parameters=ReplyParameters(
            message_id=msg.message_id, allow_sending_without_reply=True,
        ),
    )
    file = BufferedInputFile(made.data, filename="reversed.mp4")
    seconds = max(1, round(made.seconds))
    if kind == "gif":
        return await rt.bot.send_animation(
            animation=file, duration=seconds, width=made.width,
            height=made.height, **common,
        )
    if kind == "video note":
        return await rt.bot.send_video_note(
            video_note=file, duration=seconds,
            length=min(made.width, made.height), **common,
        )
    return await rt.bot.send_video(
        video=file, duration=seconds, width=made.width, height=made.height,
        supports_streaming=True, **common,
    )


async def make_reverse(rt: Runtime, msg: Message, *, mute: bool = False) -> str | None:
    """Reverse the video this message is about and send it back as a reply.

    Returns the line to keep in the chat's history (what was done, or the
    sentence they were told instead), or None when nothing visible happened (a
    rate limit already said so)."""
    found = find_video(msg)
    if found is None:
        return await _say(msg, NO_VIDEO)
    media, holder = found
    # What Telegram says of the file settles most refusals before a download.
    if media.size and media.size > video_reverse.MAX_INPUT_BYTES:
        return await _say(msg, TOO_BIG)
    if media.seconds and media.seconds > video_reverse.MAX_SECONDS:
        return await _say(msg, too_long(media.seconds))
    if await over_limit(rt, msg, "reverse"):
        return None
    try:
        async with _acting(rt.bot, msg.chat.id, "upload_video"):
            try:
                data = await vision._download_video(rt.bot, media.source_file_id)
            except Exception as exc:
                log.info("download for a reversal failed in %s: %s: %s", msg.chat.id,
                         type(exc).__name__, exc)
                _refund(rt, msg)
                return await _say(msg, DOWNLOAD_FAILED)
            if data is None:
                _refund(rt, msg)
                return await _say(msg, TOO_BIG)
            made = await video_reverse.reverse(data, mute=mute)
    except video_reverse.Refused as exc:
        if exc.code in _NOTHING_DONE:
            _refund(rt, msg)
        if exc.code == "too-long":
            return await _say(msg, too_long(float(exc.detail or 0)))
        return await _say(msg, _REFUSALS.get(exc.code, FAILED))
    except Exception as exc:
        log.warning("reversing a video failed in %s: %s: %s", msg.chat.id,
                    type(exc).__name__, exc)
        return await _say(msg, FAILED)
    try:
        sent = await _send(rt, msg, media.kind, made)
    except Exception as exc:
        log.warning("reversed video send failed in %s: %s: %s", msg.chat.id,
                    type(exc).__name__, exc)
        why = re.sub(r"\s+", " ", str(exc)).strip()[:120]
        return await _say(msg, f"Telegram wouldn't take it: {why}" if why
                          else "Telegram wouldn't take it.")
    track(msg.chat.id, getattr(sent, "message_id", None), "[reversed video]")
    return "[reversed a video]" + (" with the sound left out" if mute else "")
