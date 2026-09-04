"""Is this message for the bot, when nobody said its name?

The response policy knows two ways of being addressed: the bot's name in
the text, or a Telegram reply to one of its messages. Real conversation
has a third. Dale says something, and the next line is "why?" or "you're
making that up" or "source?" — aimed squarely at him, naming nobody.
Under a `mention` policy those went unanswered, which reads as the bot
ignoring people.

Two layers, cheapest first:

  * a conversation window: the bot counts as "in the conversation" for a
    few minutes after it last replied, while only a handful of messages
    have gone by. Inside the window a crisp follow-up opener ("why",
    "what do you mean", "prove it") is a reply to him, no model needed;
  * a cheap classifier for the genuinely ambiguous cases — a "you" that
    might mean him, a question to the room — with the last few lines of
    chat so it can see who was talking to whom.

The classifier is the only part that costs anything, and it only runs
inside the window or on an explicit question-to-the-room, so a busy chat
where Dale is quiet spends nothing.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# What the persona answers to. The name regex in handlers/chat.py lists
# the aliases; the classifier only needs the one people actually use.
BOT_NAME = "Dale"

# The bot is "in the conversation" this long after its last reply, and only
# while this few messages have gone by since — after a dozen lines between
# two other people, "you" almost never means the bot any more.
_WINDOW_SECONDS = 600
_WINDOW_MESSAGES = 6


@dataclass
class _ChatWindow:
    last_reply_at: float = 0.0
    since_reply: int = field(default=0)


_windows: dict[int, _ChatWindow] = {}


def reset() -> None:
    """Forget every window (tests)."""
    _windows.clear()


def note_bot_reply(chat_id: int, *, now: float | None = None) -> None:
    """The bot just said something in this chat: open the window."""
    w = _windows.setdefault(chat_id, _ChatWindow())
    w.last_reply_at = time.time() if now is None else now
    w.since_reply = 0


def note_user_message(chat_id: int) -> None:
    """One more line has gone by since the bot last spoke."""
    w = _windows.get(chat_id)
    if w is not None:
        w.since_reply += 1


def in_conversation(chat_id: int, *, now: float | None = None) -> bool:
    w = _windows.get(chat_id)
    if w is None or not w.last_reply_at:
        return False
    now = time.time() if now is None else now
    return (
        now - w.last_reply_at <= _WINDOW_SECONDS
        and w.since_reply <= _WINDOW_MESSAGES
    )


# ── the free layer ───────────────────────────────────────────────────────────

# Openers that only make sense as a reply to whoever spoke last. Inside the
# window that is the bot.
_FOLLOW_UP_RE = re.compile(
    r"^\W*(?:"
    r"why|how|how so|how come|what|wat|wut|huh|wdym|what do you mean|"
    r"since when|says who|source|sauce|proof|prove it|citation needed|"
    r"really|for real|fr|no way|nah|nope|wrong|false|cap|"
    r"explain|elaborate|go on|and|so|"
    r"is that true|are you sure|you sure|u sure|"
    r"lol what|lmao what"
    r")[\s?!.]*$"
    r"|^\W*\?+\W*$",
    re.IGNORECASE,
)

# Second person, minus the plural forms that mean the room.
_SECOND_PERSON_RE = re.compile(
    r"\b(?:you|your|you're|youre|yours|yourself|u|ur)\b", re.IGNORECASE,
)
_PLURAL_YOU_RE = re.compile(
    r"\b(?:you guys|you all|y'?all|you two|you lot|you people)\b", re.IGNORECASE,
)

# A question thrown to the room. Dale is a member of the room.
_ROOM_QUESTION_RE = re.compile(
    r"\b(?:"
    r"any(?:one|body)(?: here)? (?:know|remember|seen|got|have)|"
    r"does any(?:one|body)|do any of you|"
    r"can (?:some|any)(?:one|body)|could (?:some|any)(?:one|body)|"
    r"(?:some|any)(?:one|body) (?:tell|explain|remind)|"
    r"what do (?:we|people) think|"
    r"who (?:knows|remembers|can)"
    r")(?!\w)"
    r"|\b(?:thoughts|opinions)\?",
    re.IGNORECASE,
)

# "@someone …" — aimed at a named person who is not the bot (an @ of the
# bot itself is caught upstream by the mention check).
_AT_SOMEONE_RE = re.compile(r"^\s*@\w+")


def quick_verdict(text: str, *, in_conversation: bool) -> bool | None:
    """True / False when the text settles it, None when a model should look.

    Kept narrow on purpose: every None costs a classifier call, every
    wrong True is the bot butting in. Inside the window the bar is low —
    he was just talking — outside it only an explicit question to the room
    earns a look.
    """
    text = (text or "").strip()
    if not text:
        return False
    if _AT_SOMEONE_RE.match(text):
        return False
    if in_conversation:
        if _FOLLOW_UP_RE.match(text):
            return True
        if _PLURAL_YOU_RE.search(text):
            return None
        if _SECOND_PERSON_RE.search(text) or "?" in text:
            return None
        return False
    if _ROOM_QUESTION_RE.search(text):
        return None
    return False


# ── the model layer ──────────────────────────────────────────────────────────

_CLASSIFY_PROMPT = (
    "A group chat has a member named {bot}. Decide whether the NEW message "
    "is talking to {bot} or asking for {bot}'s input: a follow-up to "
    "something {bot} just said, a question only {bot} would answer, a "
    "question to the whole room, or a 'you' that means {bot}. A message "
    "aimed at some other named person, or ordinary chatter between other "
    "people, is NO.\n\n"
    "Recent messages, oldest first ({bot}'s own lines are marked):\n"
    "{recent}\n\n"
    "NEW message from {speaker}: {text}\n\n"
    "Answer with exactly one word: YES or NO."
)


def _parse_yes(answer: str | None) -> bool:
    return bool(answer) and answer.strip().upper().startswith("YES")


async def classify(
    openai, *, speaker: str, text: str, recent: list[tuple[str, str]],
    chat_id: int | None = None, bot: str = BOT_NAME,
) -> bool:
    """One cheap yes/no. Fails closed: no answer means not addressed."""
    lines = "\n".join(
        f"[{who}]: {what[:200]}" for who, what in recent[-6:]
    ) or "(nothing yet)"
    prompt = _CLASSIFY_PROMPT.format(
        bot=bot, recent=lines, speaker=speaker or "someone", text=text[:500],
    )
    try:
        answer = await openai.cheap_completion(
            prompt, max_tokens=3, temperature=0.0, chat_id=chat_id,
        )
    except Exception as exc:                      # pragma: no cover - defensive
        log.info("addressed classifier failed in %s: %s", chat_id, exc)
        return False
    return _parse_yes(answer)


async def wants_reply(
    rt, chat_id: int, *, speaker: str | None, text: str, memory_enabled: bool,
) -> bool:
    """Does this un-named, un-replied message want the bot to answer?"""
    verdict = quick_verdict(text, in_conversation=in_conversation(chat_id))
    if verdict is not None:
        return verdict
    recent: list[tuple[str, str]] = []
    if memory_enabled:
        try:
            rows = await rt.memory.recent_messages(chat_id, _WINDOW_MESSAGES)
        except Exception as exc:                  # pragma: no cover - defensive
            log.info("addressed: recent_messages failed in %s: %s", chat_id, exc)
            rows = []
        for m in rows:
            who = BOT_NAME if m.role == "assistant" else (m.author_name or "someone")
            recent.append((who, m.content))
    hit = await classify(
        rt.openai, speaker=speaker or "someone", text=text, recent=recent,
        chat_id=chat_id,
    )
    log.debug("addressed classifier in %s: %r -> %s", chat_id, text[:60], hit)
    return hit
