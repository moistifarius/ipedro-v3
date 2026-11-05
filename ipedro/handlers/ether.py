"""/ether — manually transmit a message into the ether as radio voice.

    /ether <text>             → TTS your text, drench it in radio static,
                                broadcast as a voice note to a random other
                                ether-enabled chat.
    /ether (caption on a      → take your actual recording and give it the
            voice note)         far-away-radio treatment.
    /ether (reply to a        → same, using the replied-to voice note;
            voice note)         or TTS the replied-to text if it's text.

The destination is a random ether-enabled chat other than the current
one, chosen anonymously — same conceit as the ambient loop. If ffmpeg or
TTS is unavailable the text path falls back to a garbled text broadcast.
"""

from __future__ import annotations

import io
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ipedro import ether
from ipedro.handlers.common import over_limit
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)


def _strip_command(text: str | None) -> str:
    if not text:
        return ""
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) == 2 else ""


async def _download_voice(rt: Runtime, voice) -> bytes | None:
    """Pull the raw bytes of a Telegram voice note."""
    try:
        file = await rt.bot.get_file(voice.file_id)
        buf = io.BytesIO()
        await rt.bot.download_file(file.file_path, destination=buf)
        return buf.getvalue() or None
    except Exception as exc:
        log.warning("ether: voice download failed: %s", exc)
        return None


# A transmission is a few seconds of staticky radio. A 20 MB recording used
# to be decoded whole (tens of seconds of pure-Python per-sample loops and
# hundreds of MB of float arrays) and 4000 characters of text went to TTS.
MAX_VOICE_SECONDS = 60
MAX_VOICE_BYTES = 1_500_000
MAX_TEXT_CHARS = 600


def build_router(rt: Runtime) -> Router:
    r = Router(name="ether")

    @r.message(Command("ether"))
    async def ether_cmd(msg: Message) -> None:
        reply = msg.reply_to_message

        # Resolve the audio source (own voice note → replied voice note),
        # then the text source (inline → replied text).
        voice = msg.voice or (reply.voice if reply else None)
        text = _strip_command(msg.text or msg.caption)
        if not text and not voice and reply is not None:
            text = (reply.text or reply.caption or "").strip()

        if not voice and not text:
            await msg.reply(
                "Usage: /ether <text>, or send /ether as the caption of a "
                "voice note, or reply to a voice note (or message) with "
                "/ether. It'll go out as a staticky radio transmission to "
                "another chat tuned into the ether.",
                disable_notification=True,
            )
            return

        # Who may transmit. Reaching into ANOTHER chat with an anonymous
        # voice note is for people in a chat that is itself on the ether
        # (and for bot admins): previously anyone the bot could hear, even a
        # stranger in a DM, could. Then the size/length caps and the hourly
        # limit, since this one costs a TTS call and lands in someone else's
        # chat.
        user_id = msg.from_user.id if msg.from_user else None
        if user_id not in rt.settings.admin_ids:
            cfg = (
                await rt.chats.get_config(msg.chat.id)
                if msg.chat.type != "private" else None
            )
            if cfg is None or not cfg.ether_enabled:
                await msg.reply(
                    "This chat isn't tuned into the ether, so it can't "
                    "transmit. (/chat_config ether on, by a chat admin.)",
                    disable_notification=True,
                )
                return
        if voice is not None and (
            (voice.duration or 0) > MAX_VOICE_SECONDS
            or (voice.file_size or 0) > MAX_VOICE_BYTES
        ):
            await msg.reply(
                f"That recording's too long for the radio: keep it under "
                f"{MAX_VOICE_SECONDS} seconds.",
                disable_notification=True,
            )
            return
        if text and len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS].rstrip()
        if await over_limit(rt, msg, "ether"):
            return

        voice_bytes = await _download_voice(rt, voice) if voice else None
        if voice is not None and voice_bytes is None and not text:
            await msg.reply(
                "Couldn't grab that voice note. Try again?",
                disable_notification=True,
            )
            return

        await msg.bot.send_chat_action(msg.chat.id, "record_voice")
        result = await ether.manual_broadcast(
            rt.bot, rt.db, rt.openai, msg.chat.id,
            text=text or None, voice_bytes=voice_bytes,
        )

        # Translate the structured reason code into something an admin
        # can act on. Vague "radio audio wasn't available" used to mean
        # "go grep the logs"; now the user sees the failure point.
        reason_blurb = {
            "tts_failed": "OpenAI TTS didn't return audio (quota? auth?)",
            "fx_failed": "ffmpeg radio FX produced no audio (ffmpeg missing? DSP error?)",
            "voice_send_failed": "Telegram rejected the voice send",
            "text_send_failed": "Telegram rejected the text fallback too",
            "no_text_to_garble": "the recording couldn't be processed and there's no text",
        }

        if result.mode == "voice":
            await msg.reply(
                "📟 Transmitted into the ether.", disable_notification=True,
            )
        elif result.mode == "text":
            blurb = reason_blurb.get(result.reason or "", "radio audio path failed")
            await msg.reply(
                f"📟 Transmitted into the ether (as text — {blurb}). "
                f"See /logs for details.",
                disable_notification=True,
            )
        elif result.mode == "no_dest":
            await msg.reply(
                "Nothing else is tuned into the ether right now. Turn it on "
                "in another chat with /chat_config ether on (needs a second "
                "chat to receive).",
                disable_notification=True,
            )
        else:  # no_audio
            blurb = reason_blurb.get(result.reason or "", "unknown reason")
            await msg.reply(
                f"Couldn't process that for transmission ({blurb}). "
                f"See /logs for details.",
                disable_notification=True,
            )

    return r
