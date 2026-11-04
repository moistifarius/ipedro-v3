"""Polls drafted from something a member hands the bot.

``/poll Q | A | B`` has always made a poll from a question and its options,
typed out. This is the other half: give the bot the *material* and it works
out the question and the choices. The material can be

* words (a topic, a question, a list of options, a long message), typed after
  the command or after "make a poll from this";
* the message being replied to (its text, its caption, or its picture);
* a screenshot or photo sent with the request (a menu, a list, a group chat
  asking where to eat);
* a small text file.

One cheap model call turns it into JSON; what comes back is checked against
Telegram's limits in code (a question of 300 characters, 2 to 10 options of
100) because a limit asked for in a prompt is not a limit. The material is
other people's words, so the prompt treats it as data, and the poll is only
ever posted into the chat it came from.

The model call costs money, so drafted polls are rate limited (kind
``poll``). The typed ``Q | A | B`` form calls no model and is not.
"""

from __future__ import annotations

import contextlib
import functools
import io
import json
import logging
import re
from dataclasses import dataclass

from aiogram.types import Message, ReplyParameters

from ipedro import vision
from ipedro.bot_messages import track
from ipedro.handlers.common import over_limit
from ipedro.identity import DALE
from ipedro.prompts import POLL_DRAFT_PROMPT
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

# Telegram's own limits for a poll.
MAX_QUESTION = 300
MAX_OPTION = 100
MIN_OPTIONS = 2
MAX_OPTIONS = 10

_MAX_FILE_BYTES = 100_000       # a text file bigger than this isn't a menu
_MAX_MATERIAL_CHARS = 6000      # what one poll is allowed to cost in input
_MAX_REQUEST_CHARS = 500       # of a request that goes to the model
_MAX_TYPED_CHARS = 1500         # of a poll typed out whole (Q | A | B …)
_DRAFT_TOKENS = 500
_SCAN_CHARS = 300               # a request is made at the start of a message
_TEXT_SUFFIXES = (".txt", ".text", ".md", ".csv", ".log")
_TEXT_MIMES = ("text/plain", "text/markdown", "text/csv")

HINT = (
    "Poll of what? Give me the choices (/poll Question | A | B | C), reply to "
    "a message, or send a screenshot or a small .txt file with it."
)
NOTHING = (
    "Couldn't find a poll in that. Give me the choices: "
    "/poll Question | A | B | C"
)
NO_VISION = (
    "Reading pictures is switched off here, so I can't make a poll out of one."
)


@dataclass(frozen=True)
class PollDraft:
    question: str
    options: list[str]
    multiple: bool = False

    def summary(self) -> str:
        """What goes into the chat's history for the poll that was posted."""
        tail = " (several answers allowed)" if self.multiple else ""
        return f"[made a poll] {self.question} — {' | '.join(self.options)}{tail}"


@dataclass(frozen=True)
class Material:
    """What a message offered to make a poll from."""

    text: str | None = None
    image: bytes | None = None
    media_type: str | None = None
    problem: str | None = None      # said to them instead, when set


# ── recognising the ask ─────────────────────────────────────────────────────

# Words that can come before the ask without changing it. The ask has to
# START the message (after these), so "i told him to make a poll" and "did you
# make a poll" are talk about polls, not requests for one.
_FILLER = (
    r"(?:hey|hi|hello|yo|ok|okay|so|and|also|then|now|just|please|pls|plz"
    r"|(?:can|could|would|will)\s+(?:you|u)|go\s+ahead\s+and|let['’]?s|let\s+us"
    r"|i\s+(?:want|need)\s+you\s+to|i['’]?d\s+like\s+you\s+to"
    r"|you\s+(?:should|need\s+to)|try\s+to|gonna\s+need\s+you\s+to)"
)
_VERB = (
    r"(?:make|create|start|run|do|post|send|put\s+together|put\s+up|set\s+up"
    r"|build|draft|whip\s+up|throw\s+together|generate|write)"
)
_NOISE = (
    r"(?:me|us|up|out|together|a|an|one|new|quick|little|small|simple|short|fun)"
)
_NOT_AN_ASK = r"(?!\s+(?:results?|numbers|stats|status|data|winner|so\s+far|totals?|count))"
_ASK = (
    rf"(?:{_VERB}\s+(?:{_NOISE}\s+){{0,4}}poll\b{_NOT_AN_ASK}"
    r"|(?:turn|convert|make|put|change|transform)\s+[^.!?\n]{0,50}?"
    r"\b(?:into|in\s+to|to)\s+(?:an?\s+)?poll\b"
    r"|poll\s+(?:this|that|it|these|those|them"
    r"|the\s+(?:group|chat|room|guys|boys|crew|gang|team))\b"
    r"|poll\s*[:\-–—]"
    r"|poll\s+(?:about|on|for|of|from|between)\b)"
)

_CONNECTORS = re.compile(
    r"^\W*(?:from|about|on|for|of|with|using|based\s+on|out\s+of|over|re"
    r"|regarding|between)\b",
    re.IGNORECASE,
)
_DEICTIC = re.compile(
    r"^\W*(?:this|that|it|these|those|them|here|the\s+above|above)\b",
    re.IGNORECASE,
)
_ALL_DEICTIC = re.compile(
    r"^(?:the\s+)?(?:message|msg|text|pic|photo|picture|screenshot|image|file"
    r"|list|menu|thread|convo|conversation|chat|options?|choices?|one)$"
    r"|^(?:the\s+)?(?:\w+\s+){0,2}(?:above|below)$"
    r"|^what\s+(?:i|he|she|they|we)\s+(?:just\s+)?(?:said|sent|posted|wrote|meant)$",
    re.IGNORECASE,
)


@functools.lru_cache(maxsize=32)
def _ask_re(names: str) -> re.Pattern:
    lead = rf"(?:(?:@\w+|{names}|{_FILLER})(?![\w'’])\W*)*"
    return re.compile(rf"^\W*{lead}(?P<ask>{_ASK})", re.IGNORECASE)


@functools.lru_cache(maxsize=32)
def _tail_re(names: str) -> re.Pattern:
    return re.compile(
        r"(?:(?:^|\W+)(?:please|pls|plz|thanks|thx|ty|" + names + r"))+\W*$",
        re.IGNORECASE,
    )


def clean_request(
    text: str, *, names: str | None = None, after_ask: bool = True,
) -> str:
    """What is left of a request once the asking words are gone: "" when it
    only points at something ("this", "the message above", "pls").

    `after_ask` is True for the words that followed "make a poll" (where "from
    this" and "about the" are still part of the asking) and False for the
    arguments of /poll itself, where "on or off" is a topic and only a bare
    pointer means nothing."""
    names = names or DALE.names_pattern
    rest = text.strip()
    if after_ask:
        # One connector ("from", "about"), then one pointer ("this"), and
        # whatever is left is what they said: "on where to eat" is a topic.
        for strip in (_CONNECTORS, _DEICTIC):
            rest = re.sub(r"^[\s:;,.\-–—]+", "", rest)
            rest = strip.sub("", rest, count=1)
    rest = _tail_re(names).sub("", rest).strip(" \t\r\n:;,.-–—")
    pointer = _ALL_DEICTIC.match(rest) or (
        not after_ask and _DEICTIC.fullmatch(rest)
    )
    if pointer or not re.search(r"\w", rest):
        return ""
    return rest


def detect_poll_request(text: str | None, *, names: str | None = None) -> str | None:
    """None when `text` isn't asking for a poll; otherwise what they said
    besides the ask ("" when the ask was only "make a poll from this").

    The ask must open the message: "dale make a poll from this", "poll this:
    pizza or tacos", "turn this into a poll", "can you make us a poll about
    friday". Only the start is read, so a long message can't make the match
    expensive."""
    if not text:
        return None
    names = names or DALE.names_pattern
    head = text[:_SCAN_CHARS + _MAX_TYPED_CHARS]
    m = _ask_re(names).match(head[:_SCAN_CHARS])
    if m is None:
        return None
    return clean_request(head[m.end():m.end() + _MAX_TYPED_CHARS], names=names)


# ── the draft ───────────────────────────────────────────────────────────────

_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_BULLET = re.compile(
    r"^\s*(?:[-*•·▪◦➤>]+|\(?[0-9]{1,2}[.)\]]|\(?[a-zA-Z][.)\]])\s+",
)
_YES = {"true", "yes", "1"}


def _tidy(value: object, limit: int) -> str:
    """One line, trimmed, and no longer than Telegram allows."""
    if isinstance(value, dict):
        value = value.get("text")
    if not isinstance(value, str):
        return ""
    s = re.sub(r"[\x00-\x1f\x7f  ]+", " ", value)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def clean_draft(data: object) -> PollDraft | None:
    """A PollDraft from the model's JSON, or None if it can't be a poll."""
    if not isinstance(data, dict):
        return None
    question = _tidy(data.get("question"), MAX_QUESTION)
    raw = data.get("options")
    if not question or not isinstance(raw, list):
        return None
    options: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = _tidy(item, MAX_OPTION + 20)
        text = _tidy(_BULLET.sub("", text, count=1), MAX_OPTION)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            options.append(text)
    options = options[:MAX_OPTIONS]
    if len(options) < MIN_OPTIONS:
        return None
    flag = data.get("multiple")
    multiple = flag is True or (isinstance(flag, str) and flag.strip().lower() in _YES)
    return PollDraft(question=question, options=options, multiple=multiple)


def parse_draft(text: str | None) -> PollDraft | None:
    """Find the JSON object in a model reply (which may wrap it in a fence or
    a sentence) and check it."""
    if not text:
        return None
    body = _FENCE.sub("", text.strip())
    decoder = json.JSONDecoder()
    start = body.find("{")
    while start != -1:
        try:
            data, _ = decoder.raw_decode(body, start)
        except ValueError:
            start = body.find("{", start + 1)
            continue
        return clean_draft(data)
    return None


_TAGS = re.compile(r"<(\s*/?\s*(?:request|material)\b)", re.IGNORECASE)


def _quote(text: str, limit: int) -> str:
    """`text` as data inside the prompt's tags: bounded, and unable to close
    them early."""
    return _TAGS.sub(r"‹\1", text[:limit])


def build_prompt(request: str, material: Material | None) -> str:
    if material is None:
        body = "(none: the request itself is what to make a poll from)"
    else:
        parts = []
        if material.image is not None:
            parts.append("(the attached picture)")
        if material.text:
            label = "Its caption:\n" if material.image is not None else ""
            parts.append(label + _quote(material.text, _MAX_MATERIAL_CHARS))
        body = "\n".join(parts) or "(empty)"
    return POLL_DRAFT_PROMPT.format(
        request=_quote(request, _MAX_REQUEST_CHARS) or "(nothing typed)",
        material=body,
    )


async def draft_poll(
    openai, *, request: str, material: Material | None, chat_id: int | None = None,
) -> PollDraft | None:
    """Ask the model for a poll. None whenever it can't give one: a provider
    error, an unreadable reply, or material with nothing to vote on."""
    prompt = build_prompt(request, material)
    try:
        if material is not None and material.image is not None:
            raw = await openai.describe_images(
                [material.image], media_type=material.media_type or "image/jpeg",
                prompt=prompt, max_tokens=_DRAFT_TOKENS, chat_id=chat_id,
            )
        else:
            raw = await openai.cheap_completion(
                prompt, max_tokens=_DRAFT_TOKENS, chat_id=chat_id, temperature=0.3,
            )
    except Exception as exc:
        log.info("poll draft failed in %s: %s: %s", chat_id, type(exc).__name__, exc)
        return None
    return parse_draft(raw)


# ── what a message offers ───────────────────────────────────────────────────

def _words(m: Message) -> str:
    return (getattr(m, "text", None) or getattr(m, "caption", None) or "").strip()


def _is_text_file(doc) -> bool:
    name = (getattr(doc, "file_name", None) or "").lower()
    mime = (getattr(doc, "mime_type", None) or "").lower()
    return name.endswith(_TEXT_SUFFIXES) or mime in _TEXT_MIMES


async def _read_text_file(bot, doc) -> str | None:
    """The text of a small text document, or None if it is too big, not text,
    or can't be downloaded."""
    if (getattr(doc, "file_size", None) or 0) > _MAX_FILE_BYTES:
        return None
    try:
        file = await bot.get_file(doc.file_id)
        if (file.file_size or 0) > _MAX_FILE_BYTES:
            return None
        buf = io.BytesIO()
        await bot.download_file(file.file_path, destination=buf)
    except Exception as exc:
        log.info("text file download failed: %s: %s", type(exc).__name__, exc)
        return None
    data = buf.getvalue()
    if len(data) > _MAX_FILE_BYTES or b"\x00" in data[:4096]:
        return None
    text = data.decode("utf-8-sig", "replace")
    if text.count("�") > max(3, len(text) // 50):
        return None
    return text.strip() or None


async def _attachment(
    rt: Runtime, m: Message, *, vision_ok: bool, words: str | None,
) -> Material | None:
    """What a message's picture or file offers (with `words` alongside), or
    None when it carries no attachment at all. An attachment we can't use
    still gives the words, if there are any; otherwise a `problem` to say."""
    media = vision.extract_media(m)
    if media is None:
        return None
    fallback = Material(text=words) if words else None
    if media.kind in ("photo", "image"):
        if not vision_ok:
            return fallback or Material(problem=NO_VISION)
        got = await vision.fetch_still(rt.bot, media.file_id)
        if got is None:
            return fallback or Material(
                problem="Couldn't get that picture from Telegram. Try sending it again.",
            )
        return Material(text=words, image=got[0], media_type=got[1])
    doc = getattr(m, "document", None)
    if media.kind == "file" and doc is not None and _is_text_file(doc):
        text = await _read_text_file(rt.bot, doc)
        if text is None:
            return fallback or Material(
                problem="That file's too big or isn't text. A .txt under 100 KB works.",
            )
        return Material(text=(f"{text}\n\n{words}" if words else text))
    return fallback or Material(
        problem=(
            f"I can't make a poll out of {media.label}. Words, a screenshot "
            "or a small .txt file, though."
        ),
    )


async def gather(rt: Runtime, msg: Message, *, vision_ok: bool) -> Material | None:
    """The material for a poll asked for on `msg`: its own picture or file
    first, then whatever it replies to. None when there is none (the typed
    request is then the whole source)."""
    own = await _attachment(rt, msg, vision_ok=vision_ok, words=None)
    if own is not None:
        return own
    quoted = getattr(msg, "reply_to_message", None)
    if quoted is None:
        return None
    words = _words(quoted)
    got = await _attachment(rt, quoted, vision_ok=vision_ok, words=words or None)
    if got is not None:
        return got
    return Material(text=words) if words else None


# ── the whole job ───────────────────────────────────────────────────────────

async def _say(msg: Message, text: str) -> str:
    with contextlib.suppress(Exception):
        await msg.reply(text, disable_notification=True)
    return text


def piped(request: str) -> PollDraft | None:
    """A poll typed out whole ("where to eat | pizza | tacos"), as /poll has
    always taken it, or None when `request` isn't one."""
    if "|" not in request:
        return None
    parts = [p.strip() for p in request.split("|") if p.strip()]
    if len(parts) < 3:
        return None
    return clean_draft({"question": parts[0], "options": parts[1:]})


async def _post(rt: Runtime, msg: Message, draft: PollDraft) -> str:
    # bot.send_poll with explicit reply parameters, not msg.reply_poll: that
    # one sends the deprecated allow_sending_without_reply next to its own
    # reply parameters, and Telegram goes by the latter, which don't carry it,
    # so a command deleted a moment too soon would fail the whole poll.
    try:
        sent = await rt.bot.send_poll(
            chat_id=msg.chat.id,
            message_thread_id=(
                getattr(msg, "message_thread_id", None)
                if getattr(msg, "is_topic_message", False) else None
            ),
            question=draft.question, options=draft.options, is_anonymous=True,
            allows_multiple_answers=draft.multiple,
            reply_parameters=ReplyParameters(
                message_id=msg.message_id, allow_sending_without_reply=True,
            ),
        )
    except Exception as exc:
        log.warning("poll send failed in %s: %s: %s", msg.chat.id, type(exc).__name__, exc)
        return await _say(msg, "Made one, but Telegram wouldn't take it. Try again.")
    track(msg.chat.id, getattr(sent, "message_id", None), draft.question)
    return draft.summary()


async def make_poll(
    rt: Runtime, msg: Message, request: str, *, vision_ok: bool = True,
) -> str | None:
    """Make a poll from what `msg` gives the bot and post it as a reply.

    `request` is what they typed besides the ask. A poll typed out whole is
    posted as it is, with no model and no limit; anything else is drafted
    from the material. Returns the line to keep in the chat's history (the
    poll, or the sentence they were told instead), or None when nothing
    visible happened (a rate limit already said so)."""
    request = request.strip()
    typed = piped(request[:_MAX_TYPED_CHARS])
    if typed is not None:
        return await _post(rt, msg, typed)
    request = request[:_MAX_REQUEST_CHARS]
    material = await gather(rt, msg, vision_ok=vision_ok)
    if material is not None and material.problem:
        if not request:
            return await _say(msg, material.problem)
        material = None             # they typed enough to go on without it
    if material is None and not request:
        return await _say(msg, HINT)
    if await over_limit(rt, msg, "poll"):
        return None
    with contextlib.suppress(Exception):
        await rt.bot.send_chat_action(msg.chat.id, "typing")
    draft = await draft_poll(
        rt.openai, request=request, material=material, chat_id=msg.chat.id,
    )
    if draft is None:
        return await _say(msg, NOTHING)
    return await _post(rt, msg, draft)
