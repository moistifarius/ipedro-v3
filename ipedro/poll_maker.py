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

What KIND of poll can be said in the ask or the request ("a quiz poll", "make
it public", "pick all that apply", "closes in 10 minutes"), or as a prefix
("quiz, public: Q | A | B*"), and is applied in code whatever the model says.
When the ask leaves out what the poll is about, or says "ask me what kind", the
bot asks (a force-reply to the asker) and the asker's answer finishes the poll.
"""

from __future__ import annotations

import contextlib
import functools
import io
import json
import logging
import math
import re
import time
from dataclasses import dataclass, replace

from aiogram.methods import SendPoll
from aiogram.types import ForceReply, Message, ReplyParameters

from ipedro import vision
from ipedro.bot_messages import track
from ipedro.handlers.common import over_limit
from ipedro.identity import DALE
from ipedro.prompts import (
    POLL_ASK_MAY, POLL_ASK_NEVER, POLL_DRAFT_PROMPT, POLL_KIND_MULTIPLE,
    POLL_KIND_QUIZ, POLL_KIND_SINGLE,
)
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

# Telegram's own limits for a poll.
MAX_QUESTION = 300
MAX_OPTION = 100
MIN_OPTIONS = 2
MAX_OPTIONS = 10
MAX_EXPLANATION = 200           # a quiz's "why", shown once someone answers
MIN_OPEN_SECONDS = 5            # a poll's timer: Telegram's range
MAX_OPEN_SECONDS = 2_628_000    # about 30 days

_MAX_FILE_BYTES = 100_000       # a text file bigger than this isn't a menu
_MAX_MATERIAL_CHARS = 6000      # what one poll is allowed to cost in input
_MAX_REQUEST_CHARS = 500       # of a request that goes to the model
_MAX_TYPED_CHARS = 4096         # a whole message: what Telegram allows
_DRAFT_TOKENS = 900            # room for 10 long options in a wide script
_SCAN_CHARS = 300               # a request is made at the start of a message
_MAX_LEAD_TOKENS = 8            # filler words/names allowed before the ask
_PENDING_TTL = 180.0            # seconds an answer to a question of his is awaited
_PENDING_MAX = 200              # questions waiting on an answer, all chats
_PENDING_WITH_IMAGE_MAX = 8     # …of which may hold a picture's bytes
_TEXT_SUFFIXES = (".txt", ".text", ".md", ".csv", ".log")
_TEXT_MIMES = ("text/plain", "text/markdown", "text/csv")

# Nothing to go on: only punctuation, pipes and spaces ("???", "|||").
_NOISE_ONLY = re.compile(r"[\s|.,;:!?\-–—_*#~`'\"()\[\]{}<>/\\@&%$^+=]*")

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
ASK_TOPIC = (
    "What's the poll about? Give me the question and choices, or just a topic. "
    "Want a kind? Say quiz (one right answer), pick several, public votes, or "
    "a timer like \"closes in 10 minutes\". Otherwise it's a regular poll."
)
ASK_TOPIC_WHEN = (
    "What's the poll about, and how long should it stay open (\"10 minutes\", "
    "\"2 hours\", \"1 day\")? Add a kind if you like: quiz, pick several, public votes."
)
NO_LENGTH = "You didn't give a length, so it stays open until someone stops it."
ASK_KIND = (
    "What kind of poll? Regular (pick one), pick several, quiz (one right "
    "answer), public (shows who voted), and/or a timer like \"10 minutes\". "
    "Say as many as you like."
)
ASK_WHEN = (
    "How long should it stay open? Say something like \"10 minutes\", "
    "\"2 hours\" or \"1 day\" (5 seconds to 30 days)."
)
NO_ANSWER_MARKED = (
    "A quiz needs its right answer marked: put a * after it (Paris*) or ✓ "
    "before it, like /poll quiz: Capital of France? | Rome | Paris* | Oslo"
)
NO_QUIZ_ANSWER = (
    "I couldn't tell which answer is right for that quiz. Say it, e.g. "
    "\"quiz about capitals, Paris is right\", or mark it: Paris*"
)
DROPPED = "Okay, no poll."
FORCE_PLACEHOLDER = "your answer"


def _human_span(seconds: int) -> str:
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size and seconds % size == 0:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'}"
    return f"{seconds} seconds"


@dataclass(frozen=True)
class PollDraft:
    question: str
    options: list[str]
    multiple: bool = False
    quiz: bool = False
    correct: tuple[int, ...] = ()           # indexes of the right option(s)
    explanation: str | None = None          # a quiz's "why"
    anonymous: bool = True
    open_seconds: int | None = None         # closes itself after this long
    note: str | None = None                 # said after it is posted

    def summary(self) -> str:
        """What goes into the chat's history for the poll that was posted."""
        options = [
            f"{o} ✓" if self.quiz and i in self.correct else o
            for i, o in enumerate(self.options)
        ]
        bits = []
        if self.multiple and not self.quiz:
            bits.append("several answers allowed")
        if not self.anonymous:
            bits.append("public votes")
        if self.open_seconds:
            bits.append(f"closes in {_human_span(self.open_seconds)}")
        tail = f" ({', '.join(bits)})" if bits else ""
        what = "a quiz poll" if self.quiz else "a poll"
        return f"[made {what}] {self.question} — {' | '.join(options)}{tail}"


@dataclass(frozen=True)
class Ask:
    """The drafter wants one more detail before it can make the poll."""

    question: str


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
    r"(?:me|us|up|out|together|a|an|one|new|quick|little|small|simple|short|fun"
    r"|quiz|trivia|public|anonymous|private|timed|multiple|multi-?select"
    r"|multiselect|choice|yes/no|yes-no|true/false|regular|basic)"
)
_NOT_AN_ASK = r"(?!\s+(?:results?|numbers|stats|status|data|winner|so\s+far|totals?|count))"
# What "turn ... into a poll" is turning: a pointer or a few words ("this",
# "the options above"), never a clause, so "make a meme about people who turn
# every decision into a poll" is a meme request that merely mentions one.
_THING = r"(?:[\w'’-]+\s+){0,3}"
_ASK = (
    rf"(?:{_VERB}\s+(?P<kw>(?:{_NOISE}\s+){{0,6}})poll\b{_NOT_AN_ASK}"
    rf"|(?:turn|convert|make|put|change|transform)\s+{_THING}"
    r"(?:(?:into|in\s+to)\s+(?:an?\s+)?|to\s+an?\s+)poll\b"
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
# "this: where | pizza | tacos". A pointer opens a typed poll only when a colon
# or a spaced dash follows it; "this or that | a | b" is a question about this and that.
_POINTER_LEAD = re.compile(
    r"^(?:(?:from|about|on|for|of|with|using|based\s+on|out\s+of|over)\s+)?"
    r"(?:this|that|it|these|those|them|here|the\s+above|above)\s*(?::\s*|[\-–—]\s+)",
    re.IGNORECASE,
)


# The lead-in is a repeated group over an alternation that can match two ways
# ("rusty shackleford" is "rusty shackleford", or "rusty" then "shackleford"),
# which is exponential on a message built to fail. So the repeat is BOUNDED (a
# few filler words or names, never a long run), the gap between tokens is
# possessive (it never gives characters back, so a name made of punctuation or
# an emoji can't also be read as a gap), and only the first _SCAN_CHARS of a
# message are ever read. The "@" is left out of the gap so an @mention can
# follow a word ("hey @dale_bot make a poll").
@functools.lru_cache(maxsize=32)
def _ask_re(names: str) -> re.Pattern:
    lead = rf"(?:(?:@\w+|{names}|{_FILLER})(?![\w'’])[^\w@]*+){{0,{_MAX_LEAD_TOKENS}}}"
    return re.compile(rf"^\W*{lead}(?P<ask>{_ASK})", re.IGNORECASE)


# Politeness words and the bot's own names, found anywhere, flat: no repeated
# group, so there is nothing to backtrack over.
@functools.lru_cache(maxsize=32)
def _polite_re(names: str) -> re.Pattern:
    return re.compile(
        rf"(?<![\w'’])(?:please|pls|plz|thanks|thx|ty|{names})(?![\w'’])",
        re.IGNORECASE,
    )


def _trim(s: str) -> str:
    """`s` without the punctuation, spaces and emoji at either end. The far end
    is cut from a reversed copy: an end-anchored `\\W+$` is quadratic on a long
    run of punctuation."""
    return re.sub(r"^\W+", "", re.sub(r"^\W+", "", s)[::-1])[::-1]


def clean_request(
    text: str, *, names: str | None = None, after_ask: bool = True,
) -> str:
    """What is left of a request once the asking words are gone: "" when it
    only points at something ("this", "the message above", "pls", the bot's
    own name).

    `after_ask` is True for the words that followed "make a poll" (where "from
    this" and "about the" are still part of the asking) and False for the
    arguments of /poll itself, where "on or off" is a topic and only a bare
    pointer means nothing. A poll typed whole (it has a "|") is theirs word
    for word, bar a lead-in like "this:"."""
    names = names or DALE.names_pattern
    rest = text[:_MAX_TYPED_CHARS].strip()
    if "|" in rest:
        rest = re.sub(r"^[\s:;,]+|^[-–—]\s+", "", rest, count=1)
        return _POINTER_LEAD.sub("", rest, count=1).strip()
    if after_ask:
        # One connector ("from", "about"), then one pointer ("this"), and
        # whatever is left is what they said: "on where to eat" is a topic.
        for strip in (_CONNECTORS, _DEICTIC):
            rest = re.sub(r"^[\s:;,.\-–—]+", "", rest)
            rest = strip.sub("", rest, count=1)
    rest = rest.strip(" \t\r\n:;,.-–—")
    # Judged without the politeness and the bot's name ("from this pls dale"),
    # but handed back with them: "who is cooler, matt or dale" keeps its end.
    bare = _trim(re.sub(r"\s+", " ", _polite_re(names).sub(" ", rest)))
    pointer = _ALL_DEICTIC.match(bare) or (
        not after_ask and _DEICTIC.fullmatch(bare)
    )
    return "" if pointer or not re.search(r"\w", bare) else rest


@dataclass(frozen=True)
class PollAsk:
    rest: str       # what they said besides the ask ("" for "make a poll from this")
    ask: str        # the words between the verb and "poll" ("public quiz"): a kind
                    # sits there, while the thing in "turn the pub quiz into a
                    # poll" is a topic, and so is not in it


def detect_poll_ask(text: str | None, *, names: str | None = None) -> PollAsk | None:
    """None when `text` isn't asking for a poll; otherwise the ask and what
    they said besides it.

    The ask must open the message: "dale make a poll from this", "poll this:
    pizza or tacos", "turn this into a poll", "can you make us a quiz poll about
    friday". Only the start is read, so a long message can't make the match
    expensive."""
    if not text:
        return None
    names = names or DALE.names_pattern
    m = _ask_re(names).match(text[:_SCAN_CHARS])
    if m is None:
        return None
    return PollAsk(
        rest=clean_request(text[m.end():m.end() + _MAX_TYPED_CHARS], names=names),
        ask=(m.group("kw") or "").strip(),
    )


def detect_poll_request(text: str | None, *, names: str | None = None) -> str | None:
    """What they said besides the ask ("" when the ask was only "make a poll
    from this"), or None when `text` isn't asking for a poll."""
    found = detect_poll_ask(text, names=names)
    return None if found is None else found.rest


# ── what kind of poll ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Kind:
    quiz: bool = False
    multiple: bool | None = None     # None: not said, so the draft decides
    anonymous: bool = True
    open_seconds: int | None = None  # closes itself after this long
    clamped: bool = False            # the timer asked for was past Telegram's range
    ask_kind: bool = False           # "ask me what kind"
    ask_when: bool = False           # "timed", but no length said

    def merged(self, other: "Kind") -> "Kind":
        """This kind with `other` (said later) laid over it."""
        open_seconds = other.open_seconds or self.open_seconds
        return Kind(
            quiz=self.quiz or other.quiz,
            multiple=other.multiple if other.multiple is not None else self.multiple,
            anonymous=self.anonymous and other.anonymous,
            open_seconds=open_seconds,
            clamped=self.clamped or other.clamped,
            ask_kind=self.ask_kind or other.ask_kind,
            ask_when=(self.ask_when or other.ask_when) and open_seconds is None,
        )


_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "sixty": 60, "ninety": 90,
}
_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
    "month": 2_592_000, "months": 2_592_000,
    "year": 31_536_000, "years": 31_536_000,
}
# One length of time: digits (bounded, so a long run of them can't make the
# search quadratic) or a number word, then a unit, standing alone: "ideas" is
# not "a" + "s", and "and" is not "an" + "d".
_LONG_UNITS = "|".join(sorted((u for u in _UNIT_SECONDS if len(u) > 1), key=len, reverse=True))
_ALL_UNITS = "|".join(sorted(_UNIT_SECONDS, key=len, reverse=True))
_DUR = (
    r"(?<![\w.])(?:(?P<n>\d{1,7}(?:\.\d{1,3})?)[ \t]*(?P<u>" + _ALL_UNITS + r")"
    r"|(?P<w>half\s+an?|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"
    r"\s+(?P<u2>" + _LONG_UNITS + r"))\b"
)
_DUR_RE = re.compile(_DUR, re.IGNORECASE)
_DUR_THEN = re.compile(_DUR + r"[- ]*(?:poll|timer)\b", re.IGNORECASE)
_AND_HALF = re.compile(r"\s+and\s+a\s+half\b", re.IGNORECASE)
_JOIN = re.compile(r"\s*(?:,\s*)?(?:and\s+)?", re.IGNORECASE)
# What says a poll's length when it is in what they typed (not in an ask, a
# prefix or an answer, where a bare "10 minutes" is clear): the poll closes,
# has a timer, or it ends/lasts. "the movie ends in 5 minutes" isn't one.
_WHEN_CUE = re.compile(
    r"(?:clos(?:e|es|ed|ing)\s+(?:in|after)|auto[- ]?clos(?:e|es|ing)\s+(?:in|after)"
    r"|(?:poll|vote|voting|it)\s+(?:also\s+)?(?:ends?|ending|expires?|lasts?)\s+(?:in|after|for)"
    r"|(?:timer|time\s+limit)\s+(?:of\s+|for\s+)?)\s*",
    re.IGNORECASE,
)
_TIMED_STRICT = re.compile(
    r"\btimed\s+poll\b|\btime[- ]limited\b|\bwith\s+a\s+(?:timer|time\s+limit)\b"
    r"|\bauto[- ]?clos\w*\b",
    re.IGNORECASE,
)
_TIMED_LOOSE = re.compile(r"\btimed\b", re.IGNORECASE)

# A kind named in a list of kinds ("..., public, closes in 2 hours"): after a
# comma, "and" or "+", and at the end of the request or before another kind.
# "public transport" and "pub quiz" are topics; "bowling, quiz, karaoke" is a
# list of topics; "lunch, public" is a kind.
_LISTED = r"(?:,|&|\+)\s*"
_KIND_AFTER = (
    r"(?=\s*(?:[.!]*$|(?:[,;]|\band\b)\s*(?:public|anonymous|non-anonymous|quiz|trivia"
    r"|clos\w+|timed|several|multiple|pick)\b))"
)

_QUIZ_STRICT = re.compile(
    rf"{_LISTED}\b(?:quiz|trivia){_KIND_AFTER}"
    r"|\bquiz[- ](?:poll|style|type|mode|question)s?\b"
    r"|\b(?:as|make\s+it|like|into|be)\s+an?\s+quiz\b|\bpoll\s+quiz\b"
    r"|\btrivia[- ](?:poll|style|question)s?\b"
    r"|\b(?:with|has|have)\s+an?\s+(?:correct|right)\s+answer\b"
    r"|\bone\s+(?:right|correct)\s+answer\b",
    re.IGNORECASE,
)
_QUIZ_LOOSE = re.compile(r"\b(?:quiz|trivia)\b", re.IGNORECASE)
_PUBLIC_STRICT = re.compile(
    rf"{_LISTED}\bpublic\b{_KIND_AFTER}"
    r"|\bpublic\s+(?:poll|vote|votes|voting|ballot)\b|\bmake\s+it\s+public\b"
    r"|\b(?:non|not)[- ]?anonymous\b|\bshow(?:s|ing)?\s+(?:who|names|votes)\b"
    r"|\bsee\s+who\s+voted\b|\bvisible\s+votes?\b|\bopen\s+ballot\b",
    re.IGNORECASE,
)
_PUBLIC_LOOSE = re.compile(r"\b(?:public|non[- ]?anonymous|not\s+anonymous)\b", re.IGNORECASE)
_ANON_WORD = re.compile(r"(?<![\w-])anonymous\b", re.IGNORECASE)
_MULTIPLE_STRICT = re.compile(
    r"\b(?:pick|choose|select|check|vote\s+for)\s+(?:all|more\s+than\s+one"
    r"|multiple|several|as\s+many)\b|\ball\s+that\s+apply\b"
    r"|\b(?:multiple|several|more\s+than\s+one)\s+(?:answers?|selections?|picks?|votes?)\b"
    r"|\bmulti[- ]?select\b|\bcheckbox(?:es)?\b",
    re.IGNORECASE,
)
_MULTIPLE_LOOSE = re.compile(
    r"\b(?:multiple(?![- ]choice)|several|many|multi[- ]?select|pick\s+all|all\s+that\s+apply)\b",
    re.IGNORECASE,
)
_SINGLE = re.compile(
    r"\bsingle[- ](?:answer|choice|select)\b|\bpick\s+(?:only\s+)?one\b"
    r"|\bone\s+answer\s+only\b",
    re.IGNORECASE,
)
# "ask me what kind" is a request to be asked; "what kind of pizza" is a topic.
_ASK_KIND = re.compile(
    r"\bask\s+(?:me|us)\s+(?:what|which)\s+(?:kind|type|sort)\b"
    r"|\bask\s+(?:me|us)\b(?=\s*[,.;!?]|\s*$)"
    r"|\b(?:what|which)\s+(?:kind|type|sort)\s+of\s+poll\b"
    r"|\blet\s+me\s+(?:choose|pick|say)\s+the\s+(?:kind|type)\b",
    re.IGNORECASE,
)
_ASK_KIND_WORDS = re.compile(
    r"\bask\s+(?:me|us)(?:\s+(?:what|which)\s+(?:kind|type|sort)(?:\s+of\s+poll)?)?"
    r"|\b(?:what|which)\s+(?:kind|type|sort)\s+of\s+poll\b"
    r"|\blet\s+me\s+(?:choose|pick|say)\s+the\s+(?:kind|type)\b",
    re.IGNORECASE,
)
# A kind word with one of these just before it ("not public", "don't make it
# public", "no quiz") is the opposite of a kind.
_NEGATED = re.compile(
    r"\b(?:not|no|never|nobody|without|don'?t|dont|doesn'?t|isn'?t|aren'?t)\s+(?:\w+\s+){0,2}$",
    re.IGNORECASE,
)


def _hit(pattern: re.Pattern, text: str) -> bool:
    """Does `pattern` match somewhere in `text` that isn't negated?"""
    return any(
        not _NEGATED.search(text[max(0, m.start() - 30):m.start()])
        for m in pattern.finditer(text)
    )


def strip_ask_kind(request: str) -> str:
    """`request` without "ask me what kind": those words are an instruction to
    him, not a topic to make a poll about."""
    return _ASK_KIND_WORDS.sub(" ", request).strip(" \t\r\n:;,.-–—")


def _seconds_at(text: str, pos: int) -> tuple[float, int] | None:
    """(seconds, where it ends) for the length of time written at `pos`:
    "10 minutes", "2 hours 30 minutes", "1 hour and 30 minutes", "an hour and
    a half". None if there isn't one."""
    total, end = 0.0, None
    while True:
        m = _DUR_RE.match(text, pos)
        if m is None:
            break
        n, word = m.group("n"), (m.group("w") or "").casefold()
        amount = float(n) if n else (
            0.5 if word.startswith("half") else _NUMBER_WORDS.get(word))
        unit = _UNIT_SECONDS.get((m.group("u") or m.group("u2")).casefold())
        if amount is None or unit is None:       # a letter that only folds to one
            break
        total += amount * unit
        pos = end = m.end()
        half = _AND_HALF.match(text, pos)
        if half:
            total += 0.5 * unit
            pos = end = half.end()
        pos = _JOIN.match(text, pos).end()
    return (total, end) if end is not None else None


def _duration(text: str, *, bare: bool) -> tuple[int | None, bool]:
    """(seconds, past Telegram's range) for the timer `text` asks for. `bare`
    also reads "10 minutes" with no "closes in" before it (in an ask, a prefix
    or an answer to "how long?"); otherwise a cue is needed, so "a 5 minute
    walk" is no timer."""
    found = None
    for cue in _WHEN_CUE.finditer(text):
        found = _seconds_at(text, cue.end())
        if found:
            break
    if not found:
        m = _DUR_THEN.search(text)
        found = _seconds_at(text, m.start()) if m else None
    if not found and bare:
        m = _DUR_RE.search(text)
        found = _seconds_at(text, m.start()) if m else None
    if not found:
        return None, False
    seconds = int(found[0])
    if seconds > MAX_OPEN_SECONDS:
        return MAX_OPEN_SECONDS, True
    return max(seconds, MIN_OPEN_SECONDS), False


def parse_kind(text: str, *, loose: bool = False) -> Kind:
    """The kind of poll `text` asks for. `loose` is for words that are only
    about the kind (the ask itself, a prefix, an answer to "what kind?"), where
    a bare "quiz" or "public" means it; in a topic ("public transport", "quiz
    night") only the phrasings that name a poll kind count. A kind word that is
    negated ("not public") isn't one, and "anonymous" outranks a public cue."""
    text = text[:_MAX_TYPED_CHARS]
    quiz = _hit(_QUIZ_STRICT, text) or (loose and _hit(_QUIZ_LOOSE, text))
    public = _hit(_PUBLIC_STRICT, text) or (loose and _hit(_PUBLIC_LOOSE, text))
    if public and _hit(_ANON_WORD, text):
        public = False
    multiple: bool | None = None
    if _hit(_MULTIPLE_STRICT, text) or (loose and _hit(_MULTIPLE_LOOSE, text)):
        multiple = True
    elif _hit(_SINGLE, text):
        multiple = False
    seconds, clamped = _duration(text, bare=loose)
    timed = _TIMED_STRICT.search(text) or (loose and _TIMED_LOOSE.search(text))
    return Kind(
        quiz=bool(quiz), multiple=multiple, anonymous=not public,
        open_seconds=seconds, clamped=clamped,
        ask_kind=bool(_ASK_KIND.search(text)),
        ask_when=bool(timed) and seconds is None,
    )


_PREFIX_WORDS = frozenset((
    "quiz trivia public anonymous non-anonymous regular multiple answer answers "
    "several multi-select multiselect timed closes close closing in for after "
    "and a an half one two three four five six seven eight nine ten fifteen "
    "twenty thirty forty sixty second seconds sec secs minute minutes min mins "
    "hour hours hr hrs day days week weeks s m h d w"
).split())


def split_kind_prefix(text: str) -> tuple[str, str]:
    """("quiz, public", "Q | A | B*") for "quiz, public: Q | A | B*": the words
    before the first colon, when every one of them is a kind word and together
    they ask for something. ("", text) otherwise, so a question that happens to
    contain a colon is left alone."""
    head, colon, tail = text[:100].partition(":")
    if not colon:
        return "", text
    tokens = re.findall(r"[a-z0-9.\-]+", head.lower())
    if not tokens or not all(
        t in _PREFIX_WORDS or re.fullmatch(r"\d+(?:\.\d+)?", t) for t in tokens
    ):
        return "", text
    asks = parse_kind(head, loose=True)
    if asks == Kind() and not {"regular", "anonymous"} & set(tokens):
        return "", text
    return head.strip(), text[len(head) + 1:].lstrip()


# ── the draft ───────────────────────────────────────────────────────────────

# What a model puts in front of options in spite of being told not to. Bullets
# ("-", "•") always go. Numbers ("1.", "2)", "(3)") and letters ("a)", "(b)") go
# only when they number the WHOLE list, in order: an option that merely starts
# with a number ("24. Dezember", "3. Liga", "10. Downing Street") or a name's
# initial ("M. Jordan") is the option.
_BULLET = re.compile(r"^\s*[-*•·▪◦➤]+\s+")
_NUM_MARK = re.compile(r"^\s*\(?(\d{1,2})[.)\]]\s+")
_LETTER_MARK = re.compile(r"^\s*\(?([a-zA-Z])\)\s+")


def _strip_marks(texts: list[str]) -> list[str]:
    texts = [_BULLET.sub("", t, count=1) for t in texts]
    for pattern, nth in ((_NUM_MARK, str), (_LETTER_MARK, lambda i: chr(96 + i))):
        marks = [pattern.match(t) for t in texts]
        if (len(texts) >= 2 and all(marks)
                and all(m.group(1).lower() == nth(i) for i, m in enumerate(marks, 1))):
            return [t[m.end():] for t, m in zip(texts, marks)]
    return texts


_YES = {"true", "yes", "1"}


def _tidy(value: object, limit: int) -> str:
    """One line, trimmed, and no longer than Telegram allows."""
    if isinstance(value, dict):
        value = value.get("text")
    if isinstance(value, float) and not math.isfinite(value):
        return ""                       # NaN and Infinity are not options
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)              # options like 1, 2, 3
    if not isinstance(value, str):
        return ""
    s = re.sub(r"[\x00-\x1f\x7f  ]+", " ", value)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def _indexes(value: object, final_of: dict[int, int]) -> tuple[int, ...]:
    """The right option(s) a quiz draft names, as indexes into the cleaned
    options: ints, a list of them, or digit strings; anything that no longer
    points at an option (cut, or not there) is dropped."""
    items = value if isinstance(value, list) else [value]
    found = set()
    for item in items:
        if isinstance(item, str) and item.strip().isdigit():
            item = int(item.strip())
        if isinstance(item, int) and not isinstance(item, bool) and item in final_of:
            found.add(final_of[item])
    return tuple(sorted(found))


def clean_draft(data: object, *, strip_marks: bool = True) -> PollDraft | None:
    """A PollDraft from the model's JSON, or None if it can't be a poll.
    `strip_marks` takes off numbering a model adds; a poll somebody typed is
    left as they wrote it."""
    if not isinstance(data, dict):
        return None
    question = _tidy(data.get("question"), MAX_QUESTION)
    raw = data.get("options")
    if not question or not isinstance(raw, list):
        return None
    cleaned = [(i, t) for i, item in enumerate(raw)
               if (t := _tidy(item, MAX_OPTION + 20))]
    texts = [t for _, t in cleaned]
    if strip_marks:
        texts = _strip_marks(texts)
    options: list[str] = []
    seen: dict[str, int] = {}
    final_of: dict[int, int] = {}       # an option's place in the model's list -> in ours
    for (index, _), text in zip(cleaned, texts):
        text = _tidy(text, MAX_OPTION)
        key = text.casefold()
        if not text:
            continue
        if key not in seen:
            if len(options) >= MAX_OPTIONS:
                continue
            seen[key] = len(options)
            options.append(text)
        final_of[index] = seen[key]
    if len(options) < MIN_OPTIONS:
        return None
    flag = data.get("multiple")
    multiple = flag is True or (isinstance(flag, str) and flag.strip().lower() in _YES)
    return PollDraft(
        question=question, options=options, multiple=multiple,
        correct=_indexes(data.get("correct"), final_of),
        explanation=_tidy(data.get("explanation"), MAX_EXPLANATION) or None,
    )


def apply_kind(draft: PollDraft, kind: Kind) -> PollDraft | None:
    """`draft` as the kind that was asked for: a quiz keeps its right answer(s)
    and its "why", a regular poll drops them, and what the person said (public
    votes, a timer, one answer or several) wins over what the model guessed.
    None when a quiz has no right answer to post."""
    correct = draft.correct if kind.quiz else ()
    if kind.quiz and not correct:
        return None
    if kind.quiz:
        if kind.multiple is False:
            correct = correct[:1]
        multiple = len(correct) > 1
    else:
        multiple = kind.multiple if kind.multiple is not None else draft.multiple
    return replace(
        draft, quiz=kind.quiz, correct=correct, multiple=multiple,
        explanation=draft.explanation if kind.quiz else None,
        anonymous=kind.anonymous, open_seconds=kind.open_seconds,
        note=("Telegram's longest timer is 30 days, so that's what it got."
              if kind.clamped else None),
    )


def parse_reply(text: str | None) -> PollDraft | Ask | None:
    """What a model reply says: a poll, a question it wants answered first, or
    nothing usable. The reply may wrap its JSON in a fence or a sentence.
    Raw control characters inside a string, which a model sometimes writes, are
    tolerated; a reply nested absurdly deep is just not a poll."""
    if not text:
        return None
    decoder = json.JSONDecoder(strict=False)
    start = text.find("{")
    while start != -1:
        try:
            data, _ = decoder.raw_decode(text, start)
        except RecursionError:
            return None
        except ValueError:
            start = text.find("{", start + 1)
            continue
        draft = clean_draft(data)
        if draft is not None:
            return draft
        question = _tidy(data.get("ask"), 200) if isinstance(data, dict) else ""
        return Ask(question) if question else None
    return None


def parse_draft(text: str | None) -> PollDraft | None:
    """The poll in a model reply, or None (a question it asked doesn't count)."""
    found = parse_reply(text)
    return found if isinstance(found, PollDraft) else None


_TAGS = re.compile(r"<(\s*/?\s*(?:request|material)\b)", re.IGNORECASE)


def _quote(text: str, limit: int) -> str:
    """`text` as data inside the prompt's tags: bounded, and unable to close
    them early."""
    return _TAGS.sub(r"‹\1", text[:limit])


def build_prompt(
    request: str, material: Material | None, kind: Kind | None = None,
    may_ask: bool = False,
) -> str:
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
    kinds = ""
    if kind is not None:
        if kind.quiz:
            kinds += POLL_KIND_QUIZ.format(no_answer=(
                'reply {"ask": "which one is the right answer?"}.' if may_ask
                else 'reply {"options": []}.'))
        if kind.multiple is True:
            kinds += POLL_KIND_MULTIPLE
        elif kind.multiple is False:
            kinds += POLL_KIND_SINGLE
    return POLL_DRAFT_PROMPT.format(
        request=_quote(request, _MAX_MATERIAL_CHARS) or "(nothing typed)",
        material=body, kind=kinds,
        ask_rule=POLL_ASK_MAY if may_ask else POLL_ASK_NEVER,
    )


async def draft_poll(
    openai, *, request: str, material: Material | None, chat_id: int | None = None,
    kind: Kind | None = None, may_ask: bool = False,
) -> PollDraft | Ask | None:
    """Ask the model for a poll. None whenever it can't give one: a provider
    error, an unreadable reply, or material with nothing to vote on. An Ask
    only comes back when `may_ask`."""
    prompt = build_prompt(request, material, kind, may_ask)
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
    found = parse_reply(raw)
    return None if isinstance(found, Ask) and not may_ask else found


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
                problem="Couldn't read that picture (too big, or not a normal image format).",
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


_MARK_CHARS = "✓✔✅☑"


def _correct_mark(text: str) -> tuple[bool, str]:
    """(is it marked as the right answer, the option without the mark): a * on
    the end ("Paris*") or a tick on either end ("✓ Paris", "Paris ✅")."""
    t, marked = text.replace("\ufe0f", "").strip(), False
    if t and t[0] in _MARK_CHARS:
        t, marked = t[1:].strip(), True
    if t and t[-1] in _MARK_CHARS:
        t, marked = t[:-1].strip(), True
    end = len(t)
    while end and t[end - 1] == "*":
        end -= 1
    if 0 < end < len(t):                      # a lone "*" is an option, not a mark
        t, marked = t[:end].rstrip(), True
    return marked, t


def piped(request: str, *, quiz: bool = False) -> PollDraft | None:
    """A poll typed out whole ("where to eat | pizza | tacos"), as /poll has
    always taken it, or None when `request` isn't one. For a quiz the right
    answer is the one marked with a * or a tick."""
    if "|" not in request:
        return None
    parts = [p.strip() for p in request.split("|") if p.strip()]
    if len(parts) < 3:
        return None
    question, options, right = parts[0], parts[1:], []
    if quiz:
        marks = [_correct_mark(o) for o in options]
        right = [i for i, (marked, _) in enumerate(marks) if marked]
        options = [text for _, text in marks]
    return clean_draft(
        {"question": question, "options": options, "correct": right},
        strip_marks=False,
    )


# ── asking back ─────────────────────────────────────────────────────────────

@dataclass
class Pending:
    """A question he asked, and what he has so far, until the asker answers."""

    mode: str                   # topic | kind | when | clarify
    request: str
    ask_text: str
    material: Material | None
    question_id: int | None
    at: float
    want_when: bool = False     # the topic question also asked how long


_PENDING: dict[tuple[int, int], Pending] = {}
_now = time.monotonic
_QUIZ_ANSWER = re.compile(
    r"(?<!\bpub )(?<!\bthe )(?<!\bour )(?<!\bthis )(?<!\bnext )(?<!\blast )"
    r"\b(?:quiz|trivia)\b(?!\s+(?:night|show|team|game|league|master|bowl))",
    re.IGNORECASE,
)
# "never mind" always drops the poll. A bare "no" does too when he asked what it
# is about; to "what kind?" or "how long?" it just means no kind, no timer.
_DROP = re.compile(
    r"\W*(?:never\s*mind|nvm|cancel|forget\s*it|skip\s*it|stop|drop\s*it)"
    r"(?:\s+(?:it|that|this|the\s+poll|poll|please|pls|dale|rusty|thanks?))*\W*",
    re.IGNORECASE,
)
_NO = re.compile(
    r"\W*(?:no|nope|nah|nothing|no\s+thanks)"
    r"(?:\s+(?:thanks?|please|pls|dale|rusty|poll|the\s+poll))*\W*",
    re.IGNORECASE,
)
_CANCEL_MAX = 60


def _key(msg: Message) -> tuple[int, int] | None:
    sender = getattr(msg, "from_user", None)
    return (msg.chat.id, sender.id) if sender is not None else None


def _remember(key: tuple[int, int], pending: Pending) -> None:
    """Keep `pending`, bounded: stale questions go first, then the oldest, and
    only a few may hold a picture's bytes (3.5 MB each)."""
    for k in [k for k, p in _PENDING.items() if pending.at - p.at > _PENDING_TTL]:
        del _PENDING[k]
    _PENDING.pop(key, None)
    _PENDING[key] = pending
    while len(_PENDING) > _PENDING_MAX:
        del _PENDING[next(iter(_PENDING))]
    holders = [k for k, p in _PENDING.items() if p.material and p.material.image]
    for k in holders[:max(0, len(holders) - _PENDING_WITH_IMAGE_MAX)]:
        del _PENDING[k]


def take_pending(msg: Message) -> Pending | None:
    """The question this message answers, taken off the list. In a group the
    answer has to be a reply to the question (the force-reply makes that the
    default); in a private chat the asker's next message is the answer."""
    key = _key(msg)
    pending = _PENDING.get(key) if key is not None else None
    if pending is None:
        return None
    if _now() - pending.at > _PENDING_TTL:
        del _PENDING[key]
        return None
    if getattr(msg.chat, "type", None) != "private":
        quoted = getattr(msg, "reply_to_message", None)
        if (quoted is None or pending.question_id is None
                or getattr(quoted, "message_id", None) != pending.question_id):
            return None
    del _PENDING[key]
    return pending


def forget_pending(msg: Message) -> None:
    key = _key(msg)
    if key is not None:
        _PENDING.pop(key, None)


async def _ask_back(
    rt: Runtime, msg: Message, text: str, *, mode: str, request: str,
    ask_text: str, material: Material | None, want_when: bool = False,
) -> str | None:
    """Ask the asker `text` as a force-reply (so their answer arrives as a
    reply to it) and keep what we have until they answer."""
    key = _key(msg)
    if key is None:                         # nobody to wait for
        return await _say(msg, HINT)
    try:
        sent = await msg.reply(text, reply_markup=ForceReply(
            force_reply=True, selective=True,
            input_field_placeholder=FORCE_PLACEHOLDER,
        ))
    except Exception as exc:
        log.info("poll question failed in %s: %s: %s", msg.chat.id,
                 type(exc).__name__, exc)
        return None
    _remember(key, Pending(
        mode=mode, request=request, ask_text=ask_text, material=material,
        question_id=getattr(sent, "message_id", None), at=_now(),
        want_when=want_when,
    ))
    return text


async def continue_poll(
    rt: Runtime, msg: Message, pending: Pending, typed: str, *,
    vision_ok: bool = True,
) -> str | None:
    """Finish a poll with the answer to the question he asked. The answer says
    the topic, or the kind, or how long, or the one missing detail, by what the
    question was; he asks nothing more."""
    short = len(typed) <= _CANCEL_MAX
    no = short and _NO.fullmatch(typed) is not None
    if short and _DROP.fullmatch(typed) or (no and pending.mode in ("topic", "clarify")):
        return await _say(msg, DROPPED)
    request, answer, ask_text = pending.request, "", pending.ask_text
    if no:
        typed = ""                                  # no kind, no timer: carry on
    if pending.mode == "topic":
        request = typed
        # He told them to say "quiz": a bare quiz in the answer is the kind
        # (unless it's a pub quiz, or the answer is a whole typed poll, whose
        # question is its own business). Public and timers need their phrases,
        # so "public transport" and "10 minutes of silence" stay topics.
        if "|" not in typed and _QUIZ_ANSWER.search(typed):
            ask_text = f"{ask_text} quiz".strip()
        if pending.want_when and "|" not in typed:
            length = _DUR_RE.search(typed)
            found = _seconds_at(typed, length.start()) if length else None
            if found:
                answer = f"closes in {typed[length.start():found[1]]}"
    elif pending.mode == "clarify":
        request = f"{pending.request}\n{typed}".strip()
    elif pending.mode == "when":
        answer = f"closes in {typed}" if typed else ""
    else:
        answer = typed
    return await make_poll(
        rt, msg, request, vision_ok=vision_ok, ask_text=ask_text,
        material=pending.material, answer=answer, may_ask=False,
    )


# ── posting ─────────────────────────────────────────────────────────────────

async def _post(rt: Runtime, msg: Message, draft: PollDraft) -> str:
    # bot.send_poll with explicit reply parameters, not msg.reply_poll: that
    # one sends the deprecated allow_sending_without_reply next to its own
    # reply parameters, and Telegram goes by the latter, which don't carry it,
    # so a command deleted a moment too soon would fail the whole poll.
    thread = (
        getattr(msg, "message_thread_id", None)
        if getattr(msg, "is_topic_message", False) else None
    )
    reply = ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True)
    try:
        if draft.quiz:
            # Sent as a method object: the Bot API replaced correct_option_id
            # with correct_option_ids (April 2026) and the aiogram this runs on
            # only knows the old name, so a quiz carries both.
            sent = await rt.bot(SendPoll(
                chat_id=msg.chat.id, message_thread_id=thread,
                question=draft.question, options=draft.options,
                is_anonymous=draft.anonymous, type="quiz",
                allows_multiple_answers=True if len(draft.correct) > 1 else None,
                correct_option_id=draft.correct[0] if len(draft.correct) == 1 else None,
                correct_option_ids=list(draft.correct),
                explanation=draft.explanation, open_period=draft.open_seconds,
                reply_parameters=reply,
            ))
        else:
            sent = await rt.bot.send_poll(
                chat_id=msg.chat.id, message_thread_id=thread,
                question=draft.question, options=draft.options,
                is_anonymous=draft.anonymous,
                allows_multiple_answers=draft.multiple,
                open_period=draft.open_seconds, reply_parameters=reply,
            )
    except Exception as exc:
        log.warning("poll send failed in %s: %s: %s", msg.chat.id, type(exc).__name__, exc)
        why = re.sub(r"\s+", " ", str(exc)).strip()[:120]
        return await _say(msg, f"Telegram wouldn't take that poll: {why}" if why
                          else "Telegram wouldn't take that poll.")
    track(msg.chat.id, getattr(sent, "message_id", None), draft.question)
    if draft.note:
        await _say(msg, draft.note)
    return draft.summary()


_GATHER = object()      # make_poll reads the material off the message itself


def _finalize(draft: PollDraft, kind: Kind, may_ask: bool) -> PollDraft | None:
    """apply_kind, and a word about it when they said "timed" and then never
    gave a length (he asked once; the poll stays open until someone stops it)."""
    final = apply_kind(draft, kind)
    if final is not None and kind.ask_when and not may_ask and not final.note:
        final = replace(final, note=NO_LENGTH)
    return final


async def make_poll(
    rt: Runtime, msg: Message, request: str, *, vision_ok: bool = True,
    ask_text: str = "", material: Material | None | object = _GATHER,
    answer: str = "", may_ask: bool = True,
) -> str | None:
    """Make a poll from what `msg` gives the bot and post it as a reply.

    `request` is what they typed besides the ask, and `ask_text` the asking
    words (where "quiz" or "public" can sit). A poll typed out whole is posted
    as it is, with no model and no limit; anything else is drafted from the
    material. When there is nothing to go on, or they said "ask me what kind",
    or the drafter needs one more detail, he asks (once: `may_ask` is False for
    the follow-up, whose `answer` is read for the kind). Returns the line to
    keep in the chat's history (the poll, or the sentence they were told
    instead), or None when nothing visible happened (a rate limit already said
    so)."""
    request = request.strip()
    prefix, after = split_kind_prefix(request)
    if prefix:
        ask_text, request = f"{ask_text} {prefix}".strip(), after.strip()

    def kind_of(with_request: bool) -> Kind:
        found = parse_kind(ask_text, loose=True)
        if with_request:
            found = found.merged(parse_kind(request))
        return found.merged(parse_kind(answer, loose=True)) if answer else found

    had_pipes = "|" in request
    kind = kind_of(not had_pipes)
    if not had_pipes:
        # "ask me what kind" is for him, not a topic to make a poll about.
        request = strip_ask_kind(request)
    typed = piped(request[:_MAX_TYPED_CHARS], quiz=kind.quiz)
    if typed is not None:
        if may_ask and (kind.ask_kind or kind.ask_when):
            return await _ask_back(
                rt, msg, ASK_KIND if kind.ask_kind else ASK_WHEN,
                mode="kind" if kind.ask_kind else "when", request=request,
                ask_text=ask_text, material=None,
            )
        if kind.quiz and not typed.correct:
            return await _say(msg, NO_ANSWER_MARKED)
        return await _post(rt, msg, _finalize(typed, kind, may_ask))
    if had_pipes:                         # it had a bar but isn't a poll typed whole
        kind = kind_of(True)
    if _NOISE_ONLY.fullmatch(request):
        request = ""
    if material is _GATHER:
        material = await gather(rt, msg, vision_ok=vision_ok)
    if material is not None and material.problem:
        if not request:
            return await _say(msg, material.problem)
        material = None             # they typed enough to go on without it
    if material is None and not request:
        if may_ask:
            return await _ask_back(
                rt, msg, ASK_TOPIC_WHEN if kind.ask_when else ASK_TOPIC,
                mode="topic", request="", ask_text=ask_text, material=None,
                want_when=kind.ask_when,
            )
        return await _say(msg, HINT)
    if may_ask and (kind.ask_kind or kind.ask_when):
        return await _ask_back(
            rt, msg, ASK_KIND if kind.ask_kind else ASK_WHEN,
            mode="kind" if kind.ask_kind else "when", request=request,
            ask_text=ask_text, material=material,
        )
    # Typed words that come with other material are a steer; typed words on
    # their own ARE the material (a pasted list, a message), and get its room.
    request = request[:_MAX_REQUEST_CHARS if material is not None else _MAX_MATERIAL_CHARS]
    if await over_limit(rt, msg, "poll"):
        return None
    with contextlib.suppress(Exception):
        await rt.bot.send_chat_action(msg.chat.id, "typing")
    found = await draft_poll(
        rt.openai, request=request, material=material, chat_id=msg.chat.id,
        kind=kind, may_ask=may_ask,
    )
    if isinstance(found, Ask):
        return await _ask_back(
            rt, msg, found.question, mode="clarify", request=request,
            ask_text=ask_text, material=material,
        )
    if found is None:
        return await _say(msg, NOTHING)
    final = _finalize(found, kind, may_ask)
    if final is None:
        return await _say(msg, NO_QUIZ_ANSWER)
    return await _post(rt, msg, final)
