"""Build the AI chat-completion `messages` array for a given chat.

Combines, in priority order:
  1. The persona system prompt, then the current time, the speaker-label
     convention, the capability brief (what the bot can and can't do),
     and the standing prose-rhythm nudge.
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
from ipedro.openai_client import CACHE_BREAKPOINT
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


# Marker for the synthetic history rows written when the bot reacts to a
# message with an emoji. Defined here, next to the code that reads it back,
# so the write format and the detection can never drift apart.
REACTION_NOTE_PREFIX = "(reacted "


def reaction_note(emoji: str, text: str, limit: int = 60) -> str:
    """The history line recording that the bot reacted to a message.

    Quotes the message text rather than naming the author: user turns are
    labelled elsewhere, but the snippet is what actually lets the model tie
    the reaction to the right message.
    """
    snippet = " ".join((text or "").split())
    if len(snippet) > limit:
        snippet = snippet[:limit - 1].rstrip() + "\u2026"
    return f'{REACTION_NOTE_PREFIX}{emoji} to: "{snippet}")'


_REACTION_SYSTEM = (
    "Lines in your own history of the form '(reacted <emoji> to: \"...\")' "
    "are emoji reactions YOU added to that message in this chat. They are "
    "yours and they were deliberate. If someone asks what a reaction was "
    "about, own it and give a confident, in-character reason for choosing "
    "that emoji for that message. Never deny reacting, never say you can't "
    "see any reactions, and never claim it was random or automatic."
)


# A standing nudge on prose rhythm, not on content. Two knobs: burstiness
# (variance in sentence length) and perplexity (how unpredictable the next
# word is). Left alone, a model writes sentences of near-identical length
# reaching for the most probable word every time, which reads as flat and
# machine-made whoever the persona is. This pushes both up. Deliberately
# says nothing about voice or subject so it can't fight the persona.
_STYLE_SYSTEM = (
    "Write with burst and friction. Vary sentence length hard — a long "
    "winding one, then three words. Then one. Never let three sentences in "
    "a row share a shape or a length. Reach past the expected word for the "
    "odd, specific, slightly-wrong one: the second thing that comes to "
    "mind, not the first. No balanced pairs, no tidy parallel lists, no "
    "summing-up sentence at the end. Start somewhere unexpected and stop "
    "before it gets neat. This governs rhythm and word choice only. It "
    "never overrides your persona, your mood, or what you actually mean."
)


# ── the history window is anchored, not sliding ────────────────────────
# "The last N messages" changes its first byte on every turn, so nothing
# in it can ever be a cache prefix. Anchoring the start and letting the
# window grow to 2N before re-anchoring makes the history append-only
# between re-anchors — which is what Anthropic's automatic conversation
# breakpoint needs: last turn's write is then two blocks back, inside the
# lookback. The extra rows cost a tenth of the price; the price of a
# sliding window was all of them, every time. In-process state: a restart
# is one cache miss, not a bug.
_window_anchor: dict[int, int] = {}


def reset_windows() -> None:
    """Forget every anchor (tests)."""
    _window_anchor.clear()


def _anchored_window(recent: list[StoredMessage], chat_id: int, n: int) -> list[StoredMessage]:
    """``recent`` is the newest 2N rows, oldest first. Returns N..2N of them,
    starting at a fixed row until that row falls off the fetch."""
    if not recent:
        return recent
    ids = [m.id for m in recent]
    anchor = _window_anchor.get(chat_id)
    if anchor is None or anchor not in ids:
        anchor = ids[-n] if len(ids) >= n else ids[0]
        _window_anchor[chat_id] = anchor
    # Positional, not `id >= anchor`: the rows are already in the order the
    # model should see them, and ids need not be monotonic in that order.
    return recent[ids.index(anchor):]


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
    label, e.g. on re-record paths).

    A user turn is NEVER left unlabeled: an unlabeled turn gets merged into an
    adjacent speaker's turn (the Claude path concatenates consecutive user
    messages) and misattributed, so a nameless author falls back to 'someone'.
    """
    name = (author_name or "").strip() or "someone"
    expected = f"{name}: "
    if content.startswith(expected):
        return content
    return f"{expected}{content}"


def _retrieved_line(hit: dict) -> str:
    """Render one semantic-recall hit. A recalled MESSAGE is shown with the
    speaker's name so it can't be attributed to whoever's talking now;
    non-message refs (summaries, facts) carry no single author."""
    text = (hit.get("content") or "")[:300]
    author = (hit.get("author_name") or "").strip()
    if hit.get("ref_kind") == "message" and author:
        return f"- {author}: {text}"
    return f"- ({hit.get('ref_kind', 'note')}) {text}"


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


def _format_local_stamp(dt: datetime, tz) -> str:
    """Localized wall-clock stamp, e.g. 'Sun 21 Jun 2026, 10:47 AM PDT'.
    Short weekday + short month so it stays compact when embedded in a
    gap marker."""
    local = dt.astimezone(tz)
    return local.strftime("%a %-d %b %Y, %-I:%M %p %Z").strip()


def _gap_marker(
    prev: datetime | None, cur: datetime | None, tz,
) -> str | None:
    """An inline '[⏳ <stamp>, about N later]' marker when two consecutive
    messages are far enough apart to be worth flagging, else None.

    The stamp is the *exact* localized time of ``cur`` so the bot can
    answer 'when did Matt say that' precisely; the relative span keeps
    'how long ago' obvious without arithmetic.
    """
    prev, cur = _aware(prev), _aware(cur)
    if prev is None or cur is None:
        return None
    gap = (cur - prev).total_seconds()
    if gap < _GAP_MARK_THRESHOLD_SECONDS:
        return None
    stamp = _format_local_stamp(cur, tz)
    span = _humanize_span(gap, suffix=" later")
    return f"[⏳ {stamp}, {span}]"


# The explanation of how to read the clock and the gap markers never
# changes, so it lives in the cached prefix. Only the stamp itself is
# volatile — one short line, after the breakpoint.
_CLOCK_SYSTEM = (
    "A 'Right now it is …' line gives the current local time. Use it to "
    "judge the time of day, the date, and how long ago earlier messages "
    "were sent. Inline markers like '[⏳ Sun 14 Jun 2026, 9:12 AM PDT, "
    "about 3 days later]' show silences between messages with the exact "
    "wall-clock time of the next one. Only mention the time or date when "
    "it's actually relevant."
)


def _format_now(now: datetime, tz) -> str:
    """The 'right now it is …' stamp, localized to the bot's tz. Kept to
    the bare stamp: every byte here is re-billed at full price per reply."""
    # e.g. "Saturday, 21 June 2026, 2:47 PM PDT"
    stamp = now.astimezone(tz).strftime("%A, %-d %B %Y, %-I:%M %p %Z").strip()
    return f"Right now it is {stamp}."


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
    persona_override: str | None = None,
    capabilities: str | None = None,
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

    # ── the cacheable prefix ────────────────────────────────────────────
    # Prompt caching is a prefix match: one changed byte invalidates
    # everything after it. So the system prompt is assembled in stability
    # order — content that is byte-identical between two messages in the
    # same chat first, per-request content strictly after it — and the last
    # stable block carries the breakpoint marker that _normalize_for_claude
    # turns into cache_control. Ordering IS the optimization; the marker
    # alone would do nothing.
    #
    # This is why the clock is not up here. It reads naturally as part of
    # the preamble, but a timestamp at the front is the textbook silent
    # invalidator: every minute it changes and the whole prefix behind it
    # is re-billed at full price.
    stable: list[str] = []

    # Persona — an explicit override (e.g. impersonation mode) replaces the
    # resolved persona for this turn.
    stable.append(persona_override or resolve_persona(persona, persona_custom))
    # The user-name labeling convention, so the model can tell speakers
    # apart and address people properly.
    stable.append(_NAME_PREFIX_SYSTEM)
    # What the bot can and can't do, so it never denies an ability it has or
    # promises one it hasn't. An impersonation turn is somebody else's
    # voice, so it gets neither this nor the rhythm rule.
    if capabilities and not persona_override:
        stable.append(capabilities)
    if not persona_override:
        stable.append(_STYLE_SYSTEM)
    stable.append(_CLOCK_SYSTEM)

    # Summary and facts change only when the summarizer runs (every ~80
    # messages), so they belong inside the cached prefix rather than after
    # it: an occasional cache write beats paying full price for ~640 tokens
    # on every single reply. Both are ordered by id DESC in SQL, so the
    # rendered bytes are stable rather than incidentally ordered.
    hits: list[dict] = []
    if memory_enabled:
        summary = await store.latest_summary(chat_id)
        if summary:
            stable.append(f"Conversation summary so far:\n{summary.summary}")

        facts = await store.list_facts(chat_id, limit=20)
        if facts:
            stable.append(
                "Known durable facts about this chat:\n"
                + "\n".join(f"- {f.fact}" for f in facts)
            )

        # Semantic retrieval is keyed on THIS message, so it is volatile by
        # construction and is emitted below the breakpoint.
        if latest_user_text.strip():
            hits = await store.semantic_search(
                chat_id, latest_user_text, k=settings.semantic_retrieval_k,
            )
            hits = [h for h in hits if h.get("similarity", 0) >= 0.25]

    stable_end = -1
    for block in stable:
        if _add({"role": "system", "content": block}):
            stable_end = len(messages) - 1
    if stable_end >= 0:
        # Mark the last block that actually survived the budget — marking one
        # the budget dropped would put the breakpoint in the wrong place.
        messages[stable_end][CACHE_BREAKPOINT] = True

    # ── the volatile tail ───────────────────────────────────────────────
    # Everything from here changes per request. It sits after the
    # breakpoint, so it costs full price but invalidates nothing.
    _add({"role": "system", "content": _format_now(now, settings.tzinfo)})
    if extra_system:
        _add({"role": "system", "content": extra_system})
    # Retrieval is embedded against the message being answered, so its top
    # hit is reliably that very message (similarity 1.0) — and the latest
    # summary and the top facts are embedded too, and already printed in
    # full above. A hit that is already in the request is 60-odd tokens
    # of pure duplicate; drop it rather than pay for it twice.
    already = "\n".join(stable) + "\n" + latest_user_text
    hits = [
        h for h in hits
        if (h.get("content") or "").strip()
        and (h.get("content") or "")[:300].strip() not in already
    ]
    if hits:
        _add({"role": "system", "content": (
            "Potentially relevant things said earlier (the name is who "
            "said it — attribute accordingly):\n"
            + "\n".join(_retrieved_line(h) for h in hits)
        )})

    if memory_enabled:

        # 5. Recent raw messages (chronological, last N). User-role turns
        # get their speaker's display name prefixed so the model can tell
        # who said what; a long silence before a message gets an inline
        # '[⏳ … later]' marker folded into its content (a system message
        # can't carry positional meaning — the Claude normalizer hoists
        # all system turns out of order — so it must live in the content).
        # Rendering happens oldest-first (gap markers need the chronological
        # neighbour), but the budget is applied NEWEST-first: when tokens
        # run out it's the oldest turns that fall off, not the fresh
        # context the model actually needs to answer.
        n = settings.context_recent_messages
        recent = _anchored_window(
            await store.recent_messages(chat_id, 2 * n), chat_id, n,
        )
        rendered: list[dict[str, Any]] = []
        prev_ts: datetime | None = None
        for m in recent:
            role = _role_for(m)
            content = (
                _label_user_content(m.content, m.author_name)
                if role == "user" else m.content
            )
            marker = _gap_marker(prev_ts, m.created_at, settings.tzinfo)
            if marker:
                content = f"{marker}\n{content}"
            prev_ts = _aware(m.created_at)
            rendered.append({"role": role, "content": content})
        kept: list[dict[str, Any]] = []
        for msg in reversed(rendered):
            cost = count_tokens(msg["content"])
            if used + cost > budget:
                break
            kept.append(msg)
            used += cost
        if len(kept) < len(rendered):
            log.debug(
                "Context budget truncated recent history for chat %s: "
                "dropped %d oldest of %d message(s).",
                chat_id, len(rendered) - len(kept), len(rendered),
            )
        kept.reverse()  # back to chronological order
        # Only mention reactions when one is actually in the window, so the
        # rule costs nothing on ordinary turns. It lives here rather than in
        # the persona because the live persona is a /master_prompt override,
        # which would silently drop anything written into personas.py.
        if any(REACTION_NOTE_PREFIX in m["content"] for m in kept):
            _add({"role": "system", "content": _REACTION_SYSTEM})
        messages.extend(kept)

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
