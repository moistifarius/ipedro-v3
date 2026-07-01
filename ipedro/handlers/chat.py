"""Ambient (non-command) message handling: memory writes + AI replies."""

from __future__ import annotations

import io
import logging
import random
import re
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import Message, ReactionTypeEmoji

from ipedro.bot_messages import track
from ipedro.chat_policy import IncomingMessage, should_respond
from ipedro.duckhunt.captcha_gen import matches as captcha_matches
from ipedro.duckhunt.debug_toggles import is_on as debug_is_on
from ipedro.duckhunt.scoring import challenge_is_over_time, over_time_line
from ipedro.duckhunt.verdicts import parse_verdict
from ipedro.handlers.common import catify, display_name, get_or_create_chat_config
from ipedro.impersonate import build_impersonation_prompt, resolve_impersonation
from ipedro.meme_finder import derive_topic_queries, find_relevant_meme
from ipedro.memory.context_builder import build_context
from ipedro.memory.summarizer import maybe_summarize
from ipedro.prompts import CAT_FACT_PROMPT, DUCK_BEF_CHALLENGE_JUDGE_PROMPT
from ipedro.reddit import detect_meme_request, fetch_meme
from ipedro.runtime import Runtime
from ipedro.user_flags import has_flag, maybe_auto_grudge

log = logging.getLogger(__name__)

# A pending bef challenge gates the chat: while one is outstanding, any
# plain text the user sends is judged as their answer. Without an upper
# bound a forgotten challenge would hijack the chat forever (most painful
# in a 1:1 DM, where there's nothing else to talk past). After this many
# seconds we treat the challenge as abandoned: clear it silently and let
# the message flow through as normal conversation.
_BEF_CHALLENGE_TTL_SECONDS = 3600  # 1h

# Name-mention triggers — when someone calls the bot by name, it tends
# to engage. The current persona is Dale (idale); legacy Boomhauer / Dude
# / Pedro aliases still match so people who knew the bot under earlier
# personas keep getting a response. Bare "dale" is allowed even though
# it's a common name — the bot can handle the occasional false hit.
_DUDE_NAME_RE = re.compile(
    r"\bdale\s+gribble\b"
    r"|\brusty\s+shackleford\b"
    r"|\bidale\b"
    r"|\bdale\b"
    r"|\bboomhauer\b"
    r"|\bboomhaur\b"           # common misspelling
    r"|\bthe\s+dude\b"
    r"|\bduder(ino)?\b"
    r"|\bel\s+duderino\b"
    r"|\bhis\s+dudeness\b"
    r"|\bpedro\b",
    re.IGNORECASE,
)

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
    "sh-sha. that was me, by the way",
    "rusty shackleford. you're welcome",
    "ahem. that was me",
    "i'm not saying it was me. but it was me",
    "you noticed. good. most don't",
    "sh-sha. credit where it's due",
    "i had a hunch",
    "pocket sand! ...also, you're welcome",
    "filed under: things i did",
    "yeah. that was me. don't tell anyone",
)

# "thanks dale" / "thanks rusty" / "thanks man" — common ways someone
# might thank the bot directly. Legacy dude/duder/pedro still match.
_THANKS_PEDRO_RE = re.compile(
    r"\b(thanks|thank\s*you|ty|tysm|cheers|thx)\b"
    r"[\s,!.]*\b(dale|rusty|idale|dude|duder|pedro|boomhauer|man)\b"
    r"|\b(dale|rusty|idale|dude|duder|pedro|boomhauer|man)\b"
    r"[\s,!.]*\b(thanks|thank\s*you|ty|cheers|thx)\b",
    re.IGNORECASE,
)
_THANKS_PEDRO_LINES = (
    "sh-sha. don't mention it",
    "rusty shackleford. at your service",
    "any time. but you didn't hear it from me",
    "noted. you're on the trusted list now",
    "no big deal. keep it between us",
    "sh-sha. of course",
    "easy. eyes peeled out there",
    "pocket sand! ...sorry. you're welcome",
    "filed under: favors rendered",
    "don't get used to it",
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
    """Kept for backwards-compat; matches Dude aliases now."""
    return bool(text) and _DUDE_NAME_RE.search(text) is not None


def _mentions_cat(text: str | None) -> bool:
    if not text:
        return False
    if _CAT_WORD_RE.search(text) is not None:
        return True
    return any(ch in _CAT_EMOJI for ch in text)


def _is_command(text: str | None) -> bool:
    return bool(text) and text.startswith("/")


def _challenge_is_stale(challenge) -> bool:
    """True if a pending bef challenge is older than the TTL.

    Tolerant of a missing/naive ``created_at``: a challenge we can't age
    is treated as fresh (never auto-cleared) so we don't drop a freshly
    issued one on a clock quirk.
    """
    created = getattr(challenge, "created_at", None)
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - created).total_seconds()
    return age > _BEF_CHALLENGE_TTL_SECONDS


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


async def _derive_meme_queries(
    rt: Runtime, chat_id: int, memory_on: bool,
) -> list[str]:
    """Distill the current conversation into up to 3 candidate search
    queries (specific → broad → synonym). Only HUMAN messages feed the
    prompt — the bot's own replies and the meme request itself would skew
    the topic. Returns [] when there's nothing to work with (memory off,
    empty chat, AI down)."""
    if not memory_on:
        return []
    try:
        recent = await rt.memory.recent_messages(chat_id, 16)
    except Exception as exc:  # pragma: no cover - defensive
        log.info("meme queries: recent_messages failed for %s: %s", chat_id, exc)
        return []
    lines = []
    for m in recent:
        content = (m.content or "").strip()
        if not content or content.startswith("/") or m.role != "user":
            continue
        # Drop the meme request(s) themselves — the topic is UNDER them.
        if detect_meme_request(content) is not None:
            continue
        lines.append(f"{m.author_name or 'someone'}: {content}")
    if not lines:
        return []
    snippet = "\n".join(lines)[-3000:]
    return await derive_topic_queries(rt.openai, snippet, chat_id=chat_id)


async def _record_bot_turn(rt: Runtime, cfg, chat_id: int, text: str) -> None:
    """Record one of the bot's meme-path outputs as an assistant turn and
    give the summarizer its usual chance to run. Without this, stored
    history shows the user's meme request going unanswered — and the model
    starts believing it ignores (or can't serve) such requests."""
    if not cfg.memory_enabled:
        return
    try:
        await rt.memory.record_message(
            chat_id=chat_id, role="assistant", content=text,
            message_id=None, user_id=None,
        )
        await maybe_summarize(rt.memory, rt.openai, rt.settings, chat_id)
    except Exception as exc:  # pragma: no cover - defensive
        log.info("meme-turn memory record failed for %s: %s", chat_id, exc)


async def _handle_meme_request(rt: Runtime, msg: Message, cfg, topic: str) -> None:
    """Serve a natural-language meme request ("give me a meme about X").

    ``topic`` is '' when the ask was deictic ("about this") — derive
    queries from the conversation. Candidates come from the topic's own
    subreddit first, then the meme subs, then sitewide; the cheap model
    judges which candidate actually matches. When the derived-topic hunt
    finds nothing, we say so and post a random pull — labeled, so it
    doesn't read like a failed relevance match. Every visible output is
    recorded to memory like any other bot reply."""
    # Local import: utility imports nothing from chat, but keeping this
    # lazy makes the no-cycle property robust to future refactors.
    from ipedro.handlers.utility import post_meme_to_chat

    await rt.bot.send_chat_action(msg.chat.id, "upload_photo")
    creds = dict(
        user_agent=rt.settings.reddit_user_agent,
        client_id=rt.settings.reddit_client_id,
        client_secret=rt.settings.reddit_client_secret,
    )
    if topic:
        # Explicit subject — judged search; a dry result is an honest
        # miss, not a cue to post something random.
        meme = await find_relevant_meme(
            rt.openai, [topic], topic_label=topic, trust_first=True,
            chat_id=msg.chat.id, **creds,
        )
        if meme is None:
            miss = (
                f"Sh-sha. Swept the feeds for “{topic}” — nothing usable. "
                f"Try different words."
            )
            await msg.reply(miss, disable_notification=True)
            await _record_bot_turn(rt, cfg, msg.chat.id, miss)
            return
    else:
        # "about this" / bare ask — derive queries from the conversation
        # and hunt with the judge. If that comes up dry, fall back to a
        # random r/popular pull, SAYING SO, so the user knows it's not a
        # failed relevance match.
        queries = await _derive_meme_queries(rt, msg.chat.id, cfg.memory_enabled)
        meme = None
        if queries:
            log.info(
                "meme request: derived queries %r for chat %s",
                queries, msg.chat.id,
            )
            meme = await find_relevant_meme(
                rt.openai, queries, topic_label=queries[0],
                trust_first=False, chat_id=msg.chat.id, **creds,
            )
        if meme is None:
            meme = await fetch_meme(**creds)
            if meme is None:
                quiet = (
                    "Sh-sha. Feeds are quiet or blocking me. Try again "
                    "in a bit."
                )
                await msg.reply(quiet, disable_notification=True)
                await _record_bot_turn(rt, cfg, msg.chat.id, quiet)
                return
            note = (
                f"Sh-sha. Nothing solid on “{queries[0]}” — wire's top "
                f"pull instead:"
                if queries else
                "Sh-sha. Couldn't read a topic off the room — wire's top "
                "pull:"
            )
            try:
                sent = await msg.reply(note, disable_notification=True)
                track(msg.chat.id, sent.message_id, note)
                await _record_bot_turn(rt, cfg, msg.chat.id, note)
            except Exception:  # pragma: no cover - defensive
                pass
    caption = await post_meme_to_chat(rt, msg, meme)
    if caption is None:
        fail = "Found one but couldn't deliver the media. Try again."
        await msg.reply(fail, disable_notification=True)
        await _record_bot_turn(rt, cfg, msg.chat.id, fail)
        return
    await _record_bot_turn(
        rt, cfg, msg.chat.id, f"[shared a meme] {caption}",
    )


def build_router(rt: Runtime) -> Router:
    r = Router(name="chat")

    # Reply-to-bot "bad bot" / "bad dale" deletion shortcut.
    @r.message(F.text.lower().in_({"bad bot", "bad pedro", "bad dude", "bad duder", "bad dale", "bad boomhauer", "bad rusty"}))
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
        # tracked challenge prompt, OR the user has a pending challenge in
        # this chat and sends any text, judge it via the AI and short-circuit
        # before any other handling (memory, AI reply, etc.).
        #
        # Two ways to surface a challenge match:
        #   1. The user formally replied to the prompt photo/message → look
        #      it up by (chat_id, prompt_message_id).
        #   2. The user typed the answer as plain text → look up by
        #      (chat_id, user_id). The bef_challenges PK guarantees only one
        #      outstanding challenge per (chat, user), so any text they send
        #      while a challenge is pending becomes the attempt. This mirrors
        #      the "Solve the challenge first" rule the bef action enforces.
        # Slash-commands are never challenge answers: a user mid-challenge
        # must still be able to run /chat_config, /help, /debug_*, etc.
        # (Registered commands route to their own handlers before this
        # catch-all, but an unrecognized /foo would otherwise fall through
        # and get judged — and worse, block the escape hatches.)
        _challenge_text = (msg.text or msg.caption or "")
        if (
            cfg.duckhunt_enabled
            and msg.from_user is not None
            and _challenge_text
            and not _is_command(_challenge_text)
        ):
            challenge = None
            if msg.reply_to_message is not None:
                challenge = await rt.duckhunt.find_bef_challenge_by_prompt(
                    msg.chat.id, msg.reply_to_message.message_id,
                )
            if challenge is None:
                challenge = await rt.duckhunt.get_bef_challenge(
                    msg.chat.id, msg.from_user.id,
                )
            # Stale challenge → abandon it and fall through to normal
            # handling instead of judging this message as an answer.
            if challenge is not None and _challenge_is_stale(challenge):
                log.info(
                    "Clearing stale bef challenge: chat=%s user=%s age>%ss",
                    msg.chat.id, challenge.user_id, _BEF_CHALLENGE_TTL_SECONDS,
                )
                await rt.duckhunt.clear_bef_challenge(
                    msg.chat.id, challenge.user_id,
                )
                challenge = None
            if challenge and challenge.user_id == msg.from_user.id:
                answer = (msg.text or msg.caption or "").strip()
                verdict: bool | None
                line: str | None
                # Debug toggles let an admin short-circuit the judge so they
                # can rapidly walk both branches of the challenge flow.
                admin_id = msg.from_user.id
                # Time limit: a late answer fails fast (no AI judge spend)
                # and clears the challenge, so the next bef earns a fresh,
                # different question — looking the old one up is useless.
                # Admins driving the pass/fail toggles bypass the clock.
                _debug_challenge = (
                    debug_is_on(admin_id, "always_pass_challenge")
                    or debug_is_on(admin_id, "always_fail_challenge")
                )
                if not _debug_challenge and challenge_is_over_time(
                    challenge.kind, challenge.created_at,
                ):
                    await rt.duckhunt.clear_bef_challenge(
                        msg.chat.id, msg.from_user.id,
                    )
                    log.info(
                        "bef challenge timed out: chat=%s user=%s kind=%s",
                        msg.chat.id, msg.from_user.id, challenge.kind,
                    )
                    await msg.reply(over_time_line(), disable_notification=True)
                    return
                if debug_is_on(admin_id, "always_pass_challenge"):
                    verdict = True
                    line = "Passed. [debug: always_pass_challenge]"
                elif debug_is_on(admin_id, "always_fail_challenge"):
                    verdict = False
                    line = "Failed. [debug: always_fail_challenge]"
                elif challenge.kind == "captcha":
                    verdict = captcha_matches(challenge.challenge, answer)
                    line = None
                else:
                    # PASS/FAIL judge — classification, not creative work.
                    ai_text = await rt.openai.cheap_chat(
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

        # Shut-up gate: silently drop everything from a shutup'd user.
        from_user_id = msg.from_user.id if msg.from_user else None
        if await has_flag(rt.db, msg.chat.id, from_user_id, "shutup"):
            return

        # Auto-grudge: insults toward the bot earn a 24h snark flag.
        if await maybe_auto_grudge(rt.db, msg.chat.id, from_user_id, text):
            log.info(
                "Auto-grudge added: chat=%s user=%s text=%r",
                msg.chat.id, from_user_id, text[:80],
            )

        # Record the inbound message (token-counted, optionally embedded).
        if cfg.memory_enabled:
            await rt.memory.record_message(
                chat_id=msg.chat.id,
                role="user",
                content=text,
                message_id=msg.message_id,
                user_id=from_user_id,
            )

        # "thanks pedro" → passive-aggressive line. Intercepts before the
        # normal flow so we don't also run an AI reply.
        if cfg.response_policy != "commands" and _THANKS_PEDRO_RE.search(text):
            line = random.choice(_THANKS_PEDRO_LINES)
            sent = await msg.reply(line, disable_notification=True)
            track(msg.chat.id, sent.message_id, line)
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
        # AI reply so the bot doesn't both fact and chat. A meme request
        # wins though — "meme about cats" wants a meme, not a cat fact.
        if _mentions_cat(text) and detect_meme_request(text) is None:
            await rt.bot.send_chat_action(msg.chat.id, "typing")
            fact = await rt.openai.cheap_completion(CAT_FACT_PROMPT, max_tokens=120)
            reply_text = catify(fact or "🐈")
            sent = await msg.reply(reply_text, disable_notification=True)
            track(msg.chat.id, sent.message_id, reply_text)
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
                track(msg.chat.id, sent.message_id, line)
                if cfg.memory_enabled:
                    await rt.memory.record_message(
                        chat_id=msg.chat.id, role="assistant", content=line,
                        message_id=sent.message_id, user_id=None,
                    )
            # Trigger background summarization opportunistically even when we don't reply.
            if cfg.memory_enabled:
                await maybe_summarize(rt.memory, rt.openai, rt.settings, msg.chat.id)
            return

        # "hey pedro give me a meme about this" → fetch a relevant meme
        # from Reddit (topic explicit, or distilled from the current
        # conversation) and post it with its top comment instead of a
        # text reply. Only fires on messages the bot was answering anyway.
        meme_topic = detect_meme_request(text)
        if meme_topic is not None:
            await _handle_meme_request(rt, msg, cfg, meme_topic)
            return

        await rt.bot.send_chat_action(msg.chat.id, "typing")

        state = await rt.persona_state.current(msg.chat.id)
        extra_bits = []
        base_extra = rt.persona_state.to_system_prompt(state)
        if base_extra:
            extra_bits.append(base_extra)
        snark_flag = await has_flag(rt.db, msg.chat.id, from_user_id, "snark")
        grudge_flag = await has_flag(rt.db, msg.chat.id, from_user_id, "grudge")
        if snark_flag or grudge_flag:
            why = "they recently insulted you" if grudge_flag else "by request"
            extra_bits.append(
                f"The user you're replying to right now is on your shit list "
                f"({why}). Be markedly snarkier and more dismissive to them, "
                f"without being cruel. Don't acknowledge the list."
            )
        extra = "\n\n".join(extra_bits) or None

        # Impersonation: "act like Luke" / "talk like Luke" / "do a Luke
        # impression" → resolve Luke to a real member and reply in their
        # voice, learned from their message history. Overrides the persona
        # for just this turn; falls through to a normal reply when there's
        # no request, no matching member, or too little history.
        persona_override = None
        impersonation = await resolve_impersonation(rt.db, msg.chat.id, text)
        if impersonation is not None:
            member, samples = impersonation
            persona_override = build_impersonation_prompt(member.name, samples)
            log.info(
                "Impersonating %s (%d samples) in chat %s.",
                member.name, len(samples), msg.chat.id,
            )

        ctx = await build_context(
            store=rt.memory,
            settings=rt.settings,
            chat_id=msg.chat.id,
            persona=cfg.persona,
            persona_custom=cfg.persona_custom,
            latest_user_text=text,
            latest_user_name=(
                display_name(msg.from_user) if msg.from_user else None
            ),
            extra_system=extra,
            memory_enabled=cfg.memory_enabled,
            persona_override=persona_override,
        )
        reply = await rt.openai.chat(
            ctx.messages, max_tokens=500, chat_id=msg.chat.id,
        )
        if not reply:
            return

        sent = await msg.answer(reply, disable_notification=True)
        track(msg.chat.id, sent.message_id, reply)

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
