"""Build the AI chat-completion `messages` array for a given chat.

Combines, in priority order:
  1. The persona system prompt.
  2. A condensed running summary (if any).
  3. Durable per-chat facts (if any).
  4. Semantically retrieved older snippets relevant to the latest user query.
  5. Recent raw messages (the tail of the conversation).

Everything is token-budgeted so we never blow past CONTEXT_MAX_TOKENS.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ipedro.config import Settings
from ipedro.db.repositories import StoredMessage
from ipedro.memory.store import MemoryStore
from ipedro.memory.tokens import count_tokens
from ipedro.personas import resolve_persona

log = logging.getLogger(__name__)

# Gaps shorter than this between consecutive messages are part of a normal
# back-and-forth and aren't worth annotating. At or above it, the bot gets
# an inline "[… later]" marker so it perceives the passage of time.
_GAP_MARK_THRESHOLD_SECONDS = 3600  # 1 hour


@dataclass
class BuiltContext:
    messages: list[dict[str, Any]]
    tokens: int


def _role_for(stored: StoredMessage) -> str:
    if stored.role in ("user", "assistant", "system"):
        return stored.role
    return "user"


_NAME_PREFIX_SYSTEM = (
    "This is a group chat with multiple users. Each user message is "
    "prefixed with the speaker's display name and a colon "
    "(e.g. 'Matt: ...'). Use those names to keep track of who said what, "
    "and address people by name when it's natural to do so. Your own "
    "replies are NOT prefixed — never write your own name as a prefix."
)


def _label_user_content(content: str, author_name: str | None) -> str:
    """Prefix a user-role message with its speaker name (one-time;
    don't double-prefix something that already starts with the same
    label, e.g. on re-record paths)."""
    name = (author_name or "").strip()
    if not name:
        return content
    expected = f"{name}: "
    if content.startswith(expected):
        return content
    return f"{expected}{content}"


def _aware(dt: datetime | None) -> datetime | None:
    """Coerce a possibly-naive datetime to UTC-aware (asyncpg gives us
    aware datetimes, but tests/legacy rows may be naive)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _humanize_span(seconds: float, *, suffix: str) -> str:
    """Render an elapsed span as a coarse human label, e.g.
    '3 hours', '2 days', '5 weeks'. ``suffix`` is appended (' later' for
    an inter-message marker, ' ago' for the silence-before-now note)."""
    seconds = max(0.0, seconds)
    minutes = seconds / 60
    hours = seconds / 3600
    days = seconds / 86400
    if hours < 1:
        n = max(1, round(minutes))
        unit = "minute"
        val = n
    elif days < 1:
        val = max(1, round(hours))
        unit = "hour"
    elif days < 14:
        val = max(1, round(days))
        unit = "day"
    elif days < 60:
        val = max(1, round(days / 7))
        unit = "week"
    elif days < 730:
        val = max(1, round(days / 30))
        unit = "month"
    else:
        val = max(1, round(days / 365))
        unit = "year"
    plural = "s" if val != 1 else ""
    return f"about {val} {unit}{plural}{suffix}"


def _gap_marker(prev: datetime | None, cur: datetime | None) -> str | None:
    """An inline '[⏳ … later]' marker when two consecutive messages are
    far enough apart to be worth flagging, else None."""
    prev, cur = _aware(prev), _aware(cur)
    if prev is None or cur is None:
        return None
    gap = (cur - prev).total_seconds()
    if gap < _GAP_MARK_THRESHOLD_SECONDS:
        return None
    return f"[⏳ {_humanize_span(gap, suffix=' later')}]"


def _format_now(now: datetime, tz) -> str:
    """The 'right now it is …' system line, localized to the bot's tz."""
    local = now.astimezone(tz)
    # e.g. "Saturday, 21 June 2026, 2:47 PM PDT"
    stamp = local.strftime("%A, %-d %B %Y, %-I:%M %p %Z").strip()
    return (
        f"Right now it is {stamp}. Use this to judge the time of day, the "
        f"date, and how long ago earlier messages were sent. Inline markers "
        f"like '[⏳ about 3 days later]' show silences between messages. "
        f"Only mention the time or date when it's actually relevant."
    )


async def build_context(
    *,
    store: MemoryStore,
    settings: Settings,
    chat_id: int,
    persona: str,
    persona_custom: str | None,
    latest_user_text: str,
    latest_user_name: str | None = None,
    extra_system: str | None = None,
    memory_enabled: bool = True,
    now: datetime | None = None,
) -> BuiltContext:
    budget = settings.context_max_tokens
    messages: list[dict[str, Any]] = []
    used = 0
    now = _aware(now) or datetime.now(timezone.utc)

    def _add(msg: dict[str, Any]) -> bool:
        nonlocal used
        cost = count_tokens(msg.get("content", ""))
        if used + cost > budget:
            return False
        messages.append(msg)
        used += cost
        return True

    # 1. Persona
    persona_text = resolve_persona(persona, persona_custom)
    _add({"role": "system", "content": persona_text})
    # 1a. Current time — gives the model temporal awareness (time of day,
    # date, how long ago things happened). Always present, independent of
    # memory: knowing "now" isn't conversation history.
    _add({"role": "system", "content": _format_now(now, settings.tzinfo)})
    # 1b. Tell the model the user-name labeling convention so it can
    # distinguish speakers and address people properly. Kept compact so
    # the budget hit is small.
    _add({"role": "system", "content": _NAME_PREFIX_SYSTEM})
    if extra_system:
        _add({"role": "system", "content": extra_system})

    # When memory is disabled, skip every memory-derived layer below
    # (summary, durable facts, semantic retrieval, recent raw messages).
    # That's the user-facing semantics of the toggle: each call to the
    # model is fresh, no chat history influences it. It also stops the
    # embedding round-trip on semantic_search from churning the OpenAI
    # quota for chats that have opted out of memory.
    if memory_enabled:
        # 2. Running summary
        summary = await store.latest_summary(chat_id)
        if summary:
            _add({
                "role": "system",
                "content": f"Conversation summary so far:\n{summary.summary}",
            })

        # 3. Durable facts (compact)
        facts = await store.list_facts(chat_id, limit=20)
        if facts:
            fact_block = "Known durable facts about this chat:\n" + "\n".join(
                f"- {f.fact}" for f in facts
            )
            _add({"role": "system", "content": fact_block})

        # 4. Semantic retrieval against the latest user input
        if latest_user_text.strip():
            hits = await store.semantic_search(
                chat_id, latest_user_text, k=settings.semantic_retrieval_k,
            )
            hits = [h for h in hits if h.get("similarity", 0) >= 0.25]
            if hits:
                retrieved = "Potentially relevant prior context:\n" + "\n".join(
                    f"- ({h['ref_kind']}) {h['content'][:300]}" for h in hits
                )
                _add({"role": "system", "content": retrieved})

        # 5. Recent raw messages (chronological, last N). User-role turns
        # get their speaker's display name prefixed so the model can tell
        # who said what; a long silence before a message gets an inline
        # '[⏳ … later]' marker folded into its content (a system message
        # can't carry positional meaning — the Claude normalizer hoists
        # all system turns out of order — so it must live in the content).
        recent = await store.recent_messages(chat_id, settings.context_recent_messages)
        prev_ts: datetime | None = None
        for m in recent:
            role = _role_for(m)
            content = (
                _label_user_content(m.content, m.author_name)
                if role == "user" else m.content
            )
            marker = _gap_marker(prev_ts, m.created_at)
            if marker:
                content = f"{marker}\n{content}"
            prev_ts = _aware(m.created_at)
            if not _add({"role": role, "content": content}):
                break

    # 6. Ensure the conversation ends with a user message containing the
    # current input. When memory is enabled the just-recorded user
    # message lands here via recent_messages and we dedup; when memory is
    # disabled we'd otherwise end on a stale assistant turn (or nothing),
    # and Claude returns 400 ("conversation must end with a user message").
    if latest_user_text.strip():
        labeled_latest = _label_user_content(latest_user_text, latest_user_name)
        last = messages[-1] if messages else None
        # The recorded current turn (when memory is on) sits at the tail of
        # `recent` and may have picked up an inline '[⏳ … later]' prefix, so
        # match on suffix rather than exact equality to avoid double-adding.
        already_there = (
            last is not None
            and last.get("role") == "user"
            and last.get("content", "").endswith(labeled_latest)
        )
        if not already_there:
            _add({"role": "user", "content": labeled_latest})

    return BuiltContext(messages=messages, tokens=used)
