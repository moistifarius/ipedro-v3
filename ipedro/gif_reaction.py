"""When an unprompted Dale GIF fits the moment, and which one.

The ambient sprinkle used to roll a die on every unaddressed message and send
a RANDOM clip from the whole library: a pocket-sand GIF on "the kitchen tap is
dripping", a shotgun on a question, three in an hour. Two things were wrong:

* relevance: the clip had nothing to do with what was said;
* timing: nothing asked whether now was a moment for one.

Now a GIF goes out only if all of these hold, cheapest first:

1. the roll (chat.py's ``_DALE_GIF_PROBABILITY``) says to look at all;
2. the message itself is fair game: two words to a short sentence, no
   question, link or command, and nothing that reads as sad, scary or serious;
3. the chat is ready: no GIF here for ``AMBIENT_COOLDOWN``, not more than
   ``AMBIENT_HOURLY_MAX`` an hour, a look at most every ``CHECK_GAP``, and he
   hasn't said anything himself in the last ``QUIET_AFTER_BOT``;
4. one cheap model call, shown the last few messages and the tags the library
   really has, says a particular tag plainly fits. NONE is the usual answer, and
   a reply that isn't exactly a tag we have counts as NONE;
5. the library has a GIF for that tag that this chat hasn't just seen.

Anything that fails or errors along the way means no GIF. Money: only a message
that got through 1 to 3 costs a call (a few tokens, on the cheap model), and
at most one a minute per chat.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque

from ipedro import bot_messages, dale_gifs
from ipedro.prompts import DALE_GIF_FIT_PROMPT

log = logging.getLogger(__name__)

AMBIENT_COOLDOWN = 20 * 60      # s between unprompted GIFs in one chat
AMBIENT_HOURLY_MAX = 3
CHECK_GAP = 60.0                # s between model looks in one chat
QUIET_AFTER_BOT = 180.0         # s after he last said anything here
MAX_CHARS = 160                 # a long message is somebody saying something
_CONTEXT_MESSAGES = 4           # what he is shown before the last message
_CONTEXT_CHARS = 200
_TAGS_TTL = 600.0               # s the library's tags are remembered

# The tags an unprompted GIF may be, and what each one is for. The rest of the
# library (pocketsand, shsha, guns) belongs to the keyword bits that name it.
AMBIENT_TAGS: dict[str, str] = {
    "agree": "nodding along ('yep', 'noted'): a short, flat agreement",
    "doubt": "deeply skeptical: someone claiming something unlikely or too good to be true",
    "conspiracy": "paranoid theorist: a suspicious coincidence, a cover-up, 'they don't want you to know'",
    "paranoia": "being watched: worry that someone is spying, tracking or lying",
    "smug": "smug expert: someone proven right, 'told you so', showing off what they know",
    "expert": "the credentials flex: someone confidently explaining something",
    "panic": "alarm: a sudden mishap or bad surprise about something harmless",
    "flee": "bolting from danger ('so long, suckers'): bailing out of something awkward",
    "chaos": "things falling apart comically",
    "deadpan": "flat, unimpressed stare: something absurd nobody reacted to",
}

_SERIOUS = re.compile(
    r"\b(?:died|dead|death|dying|funeral|passed\s+away|rip|cancer|hospital|surgery"
    r"|diagnos\w*|sick|illness|chemo|suicid\w*|self[- ]harm|depress\w*|anxiety"
    r"|panic\s+attack|abus\w*|assault\w*|rape\w*|shooting|shot|stabbed|accident"
    r"|crash(?:ed)?|emergency|police|arrest\w*|divorce|broke\s+up|breakup|fired"
    r"|laid\s+off|lost\s+my\s+job|evict\w*|bankrupt\w*|miscarriage|grief|grieving"
    r"|mourning|condolences|sorry\s+for\s+your\s+loss|pray(?:ing|ers)?|tragic"
    r"|tragedy|war|bomb\w*|terror\w*|kidnap\w*|overdose|kill\s+(?:my|him|her|them)self)\b",
    re.IGNORECASE,
)
_LINKISH = re.compile(r"https?://|www\.|\bt\.me/|^/", re.IGNORECASE)
# A question needs no "?": "what time is it", "anyone know a plumber".
_QUESTION_START = re.compile(
    r"^\W*(?:what|when|where|who|whom|whose|why|how|which|is|are|was|were|do|does"
    r"|did|can|could|would|will|should|has|have|had|anyone|anybody)\b",
    re.IGNORECASE,
)
_TAG_REPLY = re.compile(r"[a-z]+")
_CHAT_TAGS = re.compile(r"<(\s*/?\s*chat\b)", re.IGNORECASE)

_now = time.monotonic
_wall = time.time
_last_ambient: dict[int, float] = {}
_last_check: dict[int, float] = {}
_ambient_times: dict[int, deque[float]] = {}
_tags_cache: tuple[float, frozenset[str]] | None = None


def forget_state() -> None:
    """Clear everything remembered (tests, and nothing else)."""
    global _tags_cache
    _last_ambient.clear()
    _last_check.clear()
    _ambient_times.clear()
    _tags_cache = None


def moment_ok(text: str) -> bool:
    """Is this message one a reaction GIF could land on? Short, a statement
    rather than a question, no link, and nothing that reads as serious."""
    t = (text or "").strip()
    return (
        len(t.split()) >= 2 and len(t) <= MAX_CHARS and "?" not in t
        and not _QUESTION_START.match(t)
        and not _LINKISH.search(t) and not _SERIOUS.search(t)
    )


def chat_ready(chat_id: int) -> bool:
    """Has this chat had enough quiet from him for another look?"""
    now = _now()
    if now - _last_ambient.get(chat_id, -1e9) < AMBIENT_COOLDOWN:
        return False
    if now - _last_check.get(chat_id, -1e9) < CHECK_GAP:
        return False
    times = _ambient_times.get(chat_id)
    if times and sum(1 for t in times if now - t < 3600) >= AMBIENT_HOURLY_MAX:
        return False
    sends = bot_messages.recent(chat_id)
    if sends and _wall() - sends[0].sent_at < QUIET_AFTER_BOT:
        return False
    return dale_gifs.pace_ok(chat_id, "")


async def available_tags(db) -> frozenset[str]:
    """The ambient tags the library actually has a GIF for."""
    global _tags_cache
    now = _now()
    if _tags_cache is not None and now - _tags_cache[0] < _TAGS_TTL:
        return _tags_cache[1]
    rows = await db.fetch("SELECT DISTINCT unnest(tags) AS tag FROM dale_gifs")
    have = frozenset(r["tag"] for r in rows if r["tag"] in AMBIENT_TAGS)
    _tags_cache = (now, have)
    return have


async def _context(db, chat_id: int, typed: str) -> str:
    """The last few messages, oldest first, ending with `typed`."""
    lines: list[str] = []
    try:
        rows = await db.fetch(
            "SELECT role, content FROM messages WHERE chat_id = $1 "
            "ORDER BY id DESC LIMIT $2", chat_id, _CONTEXT_MESSAGES + 1,
        )
    except Exception as exc:
        log.debug("gif context read failed in %s: %s", chat_id, exc)
        rows = []
    older = list(rows)
    if older and typed[:40] in (older[0]["content"] or ""):
        older = older[1:]                  # the message itself, already stored
    for row in reversed(older[:_CONTEXT_MESSAGES]):
        who = "Dale" if row["role"] == "assistant" else "someone"
        lines.append(f"{who}: {(row['content'] or '')[:_CONTEXT_CHARS]}")
    lines.append(f"someone: {typed[:_CONTEXT_CHARS]}")
    return _CHAT_TAGS.sub(r"‹\1", "\n".join(lines))


async def choose(rt, chat_id: int, typed: str, tags: frozenset[str]) -> str | None:
    """The tag a model says plainly fits `typed`, or None."""
    listing = "\n".join(f"- {t}: {AMBIENT_TAGS[t]}" for t in AMBIENT_TAGS if t in tags)
    prompt = DALE_GIF_FIT_PROMPT.format(
        tags=listing, context=await _context(rt.db, chat_id, typed),
    )
    try:
        out = await rt.openai.cheap_completion(
            prompt, max_tokens=6, temperature=0.0, chat_id=chat_id,
        )
    except Exception as exc:
        log.debug("gif fit check failed in %s: %s", chat_id, exc)
        return None
    words = _TAG_REPLY.findall((out or "").lower())
    return words[0] if words and words[0] in tags else None


async def ambient(rt, msg, typed: str) -> bool:
    """Send an unprompted Dale GIF that fits `typed`, if this is the moment
    for one. True when a GIF went out."""
    chat_id = msg.chat.id
    if not moment_ok(typed) or not chat_ready(chat_id):
        return False
    tags = await available_tags(rt.db)
    if not tags:
        return False
    now = _now()
    _last_check[chat_id] = now          # before the call: a burst looks once
    tag = await choose(rt, chat_id, typed, tags)
    if tag is None:
        return False
    gif = await dale_gifs.random_for_tag(rt.db, tag, avoid=dale_gifs.recent_ids(chat_id))
    if gif is None or await dale_gifs.send_one(rt.db, msg, gif, tag=tag) is None:
        return False
    _last_ambient[chat_id] = now
    _ambient_times.setdefault(chat_id, deque(maxlen=AMBIENT_HOURLY_MAX)).append(now)
    return True
