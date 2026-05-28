"""Ambient (non-command) message handling: memory writes + AI replies."""

from __future__ import annotations

import io
import logging
import random
import re

from aiogram import F, Router
from aiogram.types import Message, ReactionTypeEmoji

from ipedro.chat_policy import IncomingMessage, should_respond
from ipedro.duckhunt.captcha_gen import matches as captcha_matches
from ipedro.duckhunt.verdicts import parse_verdict
from ipedro.handlers.common import catify, get_or_create_chat_config
from ipedro.memory.context_builder import build_context
from ipedro.memory.summarizer import maybe_summarize
from ipedro.prompts import CAT_FACT_PROMPT, DUCK_BEF_CHALLENGE_JUDGE_PROMPT
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

_PEDRO_RE = re.compile(r"\bpedro\b", re.IGNORECASE)

# Telegram's allowed reaction emoji set (subset; the API rejects others).
_REACTION_POOL = (
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
    "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡",
    "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡",
    "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
    "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨",
    "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿",
    "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂",
    "🤷", "🤷‍♀", "😡",
)

_REACT_PROBABILITY = 0.04

_POSITIVITY_RE = re.compile(
    r"\b(thanks?|thank\s*you|ty|tysm|appreciate|love\s+(it|this|that)|"
    r"great|awesome|amazing|nice|cool|good\s+(job|idea|call)|"
    r"perfect|brilliant|genius)\b",
    re.IGNORECASE,
)
_CREDIT_PROBABILITY = 0.25
_CREDIT_LINES = (
    "you're welcome btw",
    "yeah that was me",
    "i told them to do that",
    "u can thank me later",
    "i had a hunch",
    "i may have nudged things in that direction",
    "happy to help (i was barely involved)",
    "honestly i deserve most of the credit",
    "ahem.",
    "i'll accept payment in goodwill",
)

_THANKS_PEDRO_RE = re.compile(
    r"\b(thanks|thank\s*you|ty|tysm|cheers|thx)\b[\s,!.]*\bpedro\b"
    r"|\bpedro\b[\s,!.]*\b(thanks|thank\s*you|ty|cheers|thx)\b",
    re.IGNORECASE,
)
_THANKS_PEDRO_LINES = (
    "took you long enough",
    "i mean, was there ever any doubt",
    "yeah ok",
    "what about thanks for everything else",
    "you're welcome, ungrateful as that was",
    "noted. begrudgingly accepted.",
    "i'll add it to the pile of things i've done for you",
    "wow, gratitude. how novel.",
    "save it. i'll need it later.",
)
_CAT_WORD_RE = re.compile(
    r"\b("
    r"cats?|kitt(y|ies|en|ens)|felines?|"
    r"meow(s|ed|ing)?|purr(s|ed|ing)?|"
    r"whiskers?|tabby|calico|tomcat"
    r")\b",
    re.IGNORECASE,
)
_CAT_EMOJI = frozenset("🐈🐱😺😸😹😻😼😽🙀😿😾")


def _mentions_pedro(text: str | None) -> bool:
    return bool(text) and _PEDRO_RE.search(text) is not None


def _mentions_cat(text: str | None) -> bool:
    if not text:
        return False
    if _CAT_WORD_RE.search(text) is not None:
        return True
    return any(ch in _CAT_EMOJI for ch in text)


def _is_command(text: str | None) -> bool:
    return bool(text) and text.startswith("/")


def _bot_username(rt: Runtime) -> str | None:
    me = getattr(rt.bot, "_me", None)
    if me and getattr(me, "username", None):
        return me.username
    return None


def _has_bot_mention(msg: Message, bot_username: str | None) -> bool:
    if not msg.text:
        return False
    if bot_username and f"@{bot_username.lower()}" in msg.text.lower():
        return True
    for ent in (msg.entities or []):
        if ent.type == "mention":
            if bot_username and msg.text[ent.offset:ent.offset + ent.length].lower() == f"@{bot_username.lower()}":
                return True
    return False


def _is_reply_to_bot(msg: Message, bot_id: int | None) -> bool:
    return (
        msg.reply_to_message is not None
        and msg.reply_to_message.from_user is not None
        and bot_id is not None
        and msg.reply_to_message.from_user.id == bot_id
    )


async def _transcribe_voice(rt: Runtime, msg: Message) -> str | None:
    voice = msg.voice
    if not voice:
        return None
    try:
        file = await rt.bot.get_file(voice.file_id)
        buf = io.BytesIO()
        await rt.bot.download_file(file.file_path, destination=buf)
        return await rt.openai.transcribe(buf, filename="voice.ogg")
    except Exception as exc:
        log.warning("Voice transcription failed: %s", exc)
        return None


def build_router(rt: Runtime) -> Router:
    r = Router(name="chat")

    # Reply-to-bot "bad bot" / "bad pedro" deletion shortcut.
    @r.message(F.text.lower().in_({"bad bot", "bad pedro"}))
    async def remove_message(msg: Message) -> None:
        if not msg.reply_to_message:
            return
        bot_user = await rt.bot.me()
        if msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == bot_user.id:
            try:
                await msg.reply_to_message.delete()
            except Exception as exc:
                log.info("Could not delete message: %s", exc)

    @r.message()
    async def on_message(msg: Message) -> None:
        # Skip non-text and non-voice for now.
        if not msg.chat:
            return
        cfg = await get_or_create_chat_config(rt, msg)

        # Bef-challenge solution intercept: if the message is a reply to a
        # tracked challenge prompt, judge it via the AI and short-circuit
        # before any other handling (memory, AI reply, etc.).
        if (
            cfg.duckhunt_enabled
            and msg.from_user is not None
            and msg.reply_to_message is not None
            and (msg.text or msg.caption)
        ):
            challenge = await rt.duckhunt.find_bef_challenge_by_prompt(
                msg.chat.id, msg.reply_to_message.message_id,
            )
            if challenge and challenge.user_id == msg.from_user.id:
                answer = (msg.text or msg.caption or "").strip()
                verdict: bool | None
                line: str | None
                if challenge.kind == "captcha":
                    verdict = captcha_matches(challenge.challenge, answer)
                    line = None
                else:
                    ai_text = await rt.openai.chat(
                        [{
                            "role": "user",
                            "content": DUCK_BEF_CHALLENGE_JUDGE_PROMPT.format(
                                challenge=challenge.challenge, answer=answer,
                            ),
                        }],
                        max_tokens=120, temperature=1.0,
                    )
                    verdict, line = parse_verdict(ai_text, "PASS", "FAIL")
                log.info(
                    "bef challenge judge: chat=%s user=%s kind=%s verdict=%s",
                    msg.chat.id, msg.from_user.id, challenge.kind, verdict,
                )
                if verdict is True:
                    await rt.duckhunt.clear_bef_challenge(
                        msg.chat.id, msg.from_user.id,
                    )
                    await msg.reply(
                        line or "OK, you may try `bef` again.",
                        disable_notification=True,
                    )
                else:
                    await msg.reply(
                        line or "Not quite. Try again.",
                        disable_notification=True,
                    )
                return

        # Voice notes: optionally transcribe and treat the transcript as user text.
        text = msg.text or msg.caption
        if not text and msg.voice and cfg.voice_transcribe:
            transcription = await _transcribe_voice(rt, msg)
            if transcription:
                text = f"[voice transcript] {transcription}"

        if not text:
            return  # nothing actionable

        if _is_command(text):
            # Commands are handled by their own routers; nothing to do here.
            return

        bot_me = await rt.bot.me()
        bot_id = bot_me.id
        bot_username = bot_me.username

        # Record the inbound message (token-counted, optionally embedded).
        if cfg.memory_enabled:
            await rt.memory.record_message(
                chat_id=msg.chat.id,
                role="user",
                content=text,
                message_id=msg.message_id,
                user_id=msg.from_user.id if msg.from_user else None,
            )

        # "thanks pedro" → passive-aggressive line. Intercepts before the
        # normal flow so we don't also run an AI reply.
        if cfg.response_policy != "commands" and _THANKS_PEDRO_RE.search(text):
            line = random.choice(_THANKS_PEDRO_LINES)
            sent = await msg.reply(line, disable_notification=True)
            if cfg.memory_enabled:
                await rt.memory.record_message(
                    chat_id=msg.chat.id, role="assistant", content=line,
                    message_id=sent.message_id, user_id=None,
                )
            return

        # Ambient emoji reaction (rare, never on commands or our own intercepts).
        if (
            cfg.response_policy != "commands"
            and msg.message_id
            and random.random() < _REACT_PROBABILITY
        ):
            try:
                await rt.bot.set_message_reaction(
                    chat_id=msg.chat.id,
                    message_id=msg.message_id,
                    reaction=[ReactionTypeEmoji(emoji=random.choice(_REACTION_POOL))],
                )
            except Exception as exc:
                log.debug("Reaction failed: %s", exc)

        # Cat mention: drop a dubious cat fact and stop. Skip the regular
        # AI reply so the bot doesn't both fact and chat.
        if _mentions_cat(text):
            await rt.bot.send_chat_action(msg.chat.id, "typing")
            fact = await rt.openai.short_completion(CAT_FACT_PROMPT, max_tokens=120)
            reply_text = catify(fact or "🐈")
            sent = await msg.reply(reply_text, disable_notification=True)
            if cfg.memory_enabled:
                await rt.memory.record_message(
                    chat_id=msg.chat.id,
                    role="assistant",
                    content=reply_text,
                    message_id=sent.message_id,
                    user_id=None,
                )
                await maybe_summarize(rt.memory, rt.openai, rt.settings, msg.chat.id)
            return

        incoming = IncomingMessage(
            text=text,
            has_mention_of_bot=(
                _has_bot_mention(msg, bot_username) or _mentions_pedro(text)
            ),
            is_reply_to_bot=_is_reply_to_bot(msg, bot_id),
            is_command=False,
            chat_type=msg.chat.type,
        )

        if not should_respond(
            cfg.response_policy, incoming,
            ambient_probability=cfg.ambient_probability,
        ):
            # "Taking credit": when a positive line is spotted, Pedro
            # occasionally inserts itself even when policy wouldn't reply.
            # Skipped under the explicit commands-only opt-out.
            if (
                cfg.response_policy != "commands"
                and _POSITIVITY_RE.search(text)
                and random.random() < _CREDIT_PROBABILITY
            ):
                line = random.choice(_CREDIT_LINES)
                sent = await msg.answer(line, disable_notification=True)
                if cfg.memory_enabled:
                    await rt.memory.record_message(
                        chat_id=msg.chat.id, role="assistant", content=line,
                        message_id=sent.message_id, user_id=None,
                    )
            # Trigger background summarization opportunistically even when we don't reply.
            if cfg.memory_enabled:
                await maybe_summarize(rt.memory, rt.openai, rt.settings, msg.chat.id)
            return

        await rt.bot.send_chat_action(msg.chat.id, "typing")

        state = await rt.persona_state.current(msg.chat.id)
        extra = rt.persona_state.to_system_prompt(state) or None
        ctx = await build_context(
            store=rt.memory,
            settings=rt.settings,
            chat_id=msg.chat.id,
            persona=cfg.persona,
            persona_custom=cfg.persona_custom,
            latest_user_text=text,
            extra_system=extra,
        )
        reply = await rt.openai.chat(
            ctx.messages, max_tokens=500, chat_id=msg.chat.id,
        )
        if not reply:
            return

        sent = await msg.answer(reply, disable_notification=True)

        if cfg.memory_enabled:
            await rt.memory.record_message(
                chat_id=msg.chat.id,
                role="assistant",
                content=reply,
                message_id=sent.message_id,
                user_id=None,
            )
            await maybe_summarize(rt.memory, rt.openai, rt.settings, msg.chat.id)

    return r
