"""Context builder integrates persona + summary + facts + retrieval + recent.

The DB and OpenAI dependencies are stubbed out: we only care that the
right pieces appear (in the right priority) and that the token budget caps
output.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ipedro.config import Settings
from ipedro.db.repositories import StoredFact, StoredMessage, StoredSummary
from ipedro.memory.context_builder import build_context


class FakeStore:
    def __init__(self, recent=None, summary=None, facts=None, semantic=None):
        self._recent = recent or []
        self._summary = summary
        self._facts = facts or []
        self._semantic = semantic or []
        self.recent_calls = 0
        self.summary_calls = 0
        self.facts_calls = 0
        self.semantic_calls = 0

    async def recent_messages(self, chat_id, limit):
        self.recent_calls += 1
        return self._recent[-limit:]

    async def latest_summary(self, chat_id):
        self.summary_calls += 1
        return self._summary

    async def list_facts(self, chat_id, limit=50):
        self.facts_calls += 1
        return self._facts[:limit]

    async def semantic_search(self, chat_id, query, k=6):
        self.semantic_calls += 1
        return self._semantic


def _msg(content, role="user", mid=1, author_name="Matt"):
    return StoredMessage(
        id=mid, chat_id=1, message_id=mid, user_id=42, role=role,
        content=content, tokens=None, created_at=datetime.now(timezone.utc),
        author_name=author_name if role == "user" else None,
    )


def _settings():
    return Settings(  # type: ignore[call-arg]
        telegram_bot_token="t", openai_api_key="k",
        database_url="postgresql://t/t",
        context_recent_messages=10, context_max_tokens=10_000,
        semantic_retrieval_k=4,
    )


@pytest.mark.asyncio
async def test_minimal_context_includes_persona_and_recent():
    store = FakeStore(recent=[_msg("hi there", "user"), _msg("hey back", "assistant", mid=2)])
    built = await build_context(
        store=store, settings=_settings(), chat_id=1,
        persona="pedro", persona_custom=None,
        latest_user_text="hi there",
    )
    roles = [m["role"] for m in built.messages]
    assert roles[0] == "system"  # persona
    assert "user" in roles
    assert "assistant" in roles


@pytest.mark.asyncio
async def test_summary_and_facts_appear_when_available():
    summary = StoredSummary(
        id=1, chat_id=1, summary="prior bullets",
        covers_until_id=99, created_at=datetime.now(timezone.utc),
    )
    facts = [StoredFact(
        id=10, chat_id=1, user_id=42, fact="user likes ducks",
        source_msg=None, created_at=datetime.now(timezone.utc),
    )]
    store = FakeStore(recent=[_msg("x")], summary=summary, facts=facts)
    built = await build_context(
        store=store, settings=_settings(), chat_id=1,
        persona="pedro", persona_custom=None,
        latest_user_text="x",
    )
    text_blob = "\n".join(m["content"] for m in built.messages)
    assert "prior bullets" in text_blob
    assert "user likes ducks" in text_blob


@pytest.mark.asyncio
async def test_semantic_hits_above_threshold_are_included():
    store = FakeStore(
        recent=[_msg("x")],
        semantic=[
            {"ref_kind": "message", "ref_id": 7, "content": "matt likes spicy noodles", "similarity": 0.42},
            {"ref_kind": "message", "ref_id": 8, "content": "irrelevant trivia", "similarity": 0.10},  # below threshold
        ],
    )
    built = await build_context(
        store=store, settings=_settings(), chat_id=1,
        persona="pedro", persona_custom=None,
        latest_user_text="noodles",
    )
    blob = "\n".join(m["content"] for m in built.messages)
    assert "spicy noodles" in blob
    assert "irrelevant trivia" not in blob


@pytest.mark.asyncio
async def test_semantic_message_hits_show_the_speaker():
    """A recalled MESSAGE must carry its speaker's name so it can't be pinned
    on whoever is talking now; a non-message ref (summary) stays unauthored."""
    store = FakeStore(
        recent=[_msg("x")],
        semantic=[
            {"ref_kind": "message", "ref_id": 7,
             "content": "spicy noodles are elite", "similarity": 0.5,
             "author_name": "Luke"},
            {"ref_kind": "summary", "ref_id": 9,
             "content": "the group argued about films", "similarity": 0.5,
             "author_name": None},
        ],
    )
    built = await build_context(
        store=store, settings=_settings(), chat_id=1,
        persona="pedro", persona_custom=None, latest_user_text="noodles",
    )
    blob = "\n".join(m["content"] for m in built.messages)
    assert "Luke: spicy noodles are elite" in blob
    assert "(summary) the group argued about films" in blob


@pytest.mark.asyncio
async def test_token_budget_caps_output():
    # Tiny budget should drop the recent messages from the tail.
    s = _settings()
    s.context_max_tokens = 30  # roughly enough for persona only
    huge_recent = [_msg("X" * 1000, "user", mid=i) for i in range(5)]
    store = FakeStore(recent=huge_recent)
    built = await build_context(
        store=store, settings=s, chat_id=1,
        persona="neutral", persona_custom=None,
        latest_user_text="X" * 1000,
    )
    assert built.tokens <= 30
    # Persona system message should still be present.
    assert built.messages[0]["role"] == "system"


@pytest.mark.asyncio
async def test_budget_truncation_drops_oldest_keeps_newest_in_order():
    """When the budget can't hold all recent messages, the OLDEST must
    fall off — not the newest (regression: the loop used to add
    oldest-first and break when the budget ran out, so exactly the fresh
    messages the model needed were the ones dropped). Survivors stay in
    chronological order."""
    s = _settings()
    s.context_max_tokens = 250
    old_blob = "old stuff that should be dropped " * 30  # ~240+ tokens each
    store = FakeStore(recent=[
        _msg(old_blob, "user", mid=1),
        _msg(old_blob, "user", mid=2),
        _msg("recent alpha", "user", mid=3),
        _msg("recent beta", "user", mid=4),
    ])
    built = await build_context(
        store=store, settings=s, chat_id=1,
        persona="dude", persona_custom="Test persona.",
        latest_user_text="recent beta", latest_user_name="Matt",
    )
    convo = [m["content"] for m in built.messages if m["role"] != "system"]
    # The newest messages survived…
    assert "Matt: recent alpha" in convo
    assert "Matt: recent beta" in convo
    # …the oldest were the ones dropped…
    assert all("old stuff" not in c for c in convo)
    # …and the kept tail is back in chronological order, within budget.
    assert convo.index("Matt: recent alpha") < convo.index("Matt: recent beta")
    assert built.tokens <= s.context_max_tokens


@pytest.mark.asyncio
async def test_custom_persona_takes_precedence():
    store = FakeStore(recent=[_msg("hi")])
    built = await build_context(
        store=store, settings=_settings(), chat_id=1,
        persona="pedro", persona_custom="You are a butler named Reginald.",
        latest_user_text="hi",
    )
    assert "Reginald" in built.messages[0]["content"]


# --------------------------------- ends-with-user-message invariant ---------
@pytest.mark.asyncio
async def test_messages_always_end_with_user_when_text_is_set():
    """Anthropic rejects (HTTP 400) any chat that doesn't end on a user
    message. build_context must guarantee that — even if recent_messages
    happens to return an assistant turn as its last item, or returns
    nothing at all."""
    # Last recorded turn is the bot's prior reply.
    store = FakeStore(recent=[_msg("you ok?", role="user"),
                              _msg("yeah man.", role="assistant")])
    built = await build_context(
        store=store, settings=_settings(), chat_id=1,
        persona="dude", persona_custom=None,
        latest_user_text="what time is it", latest_user_name="Bob",
    )
    assert built.messages[-1]["role"] == "user"
    assert built.messages[-1]["content"] == "Bob: what time is it"


@pytest.mark.asyncio
async def test_messages_do_not_duplicate_existing_user_turn():
    """When memory is on, the just-recorded user message already lives at
    the tail of recent_messages — appending it again would inflate the
    context and double-bill tokens. Dedup compares the LABELED forms so
    the recent-message label matches the final-turn label."""
    store = FakeStore(recent=[_msg("yo", author_name="Matt")])
    built = await build_context(
        store=store, settings=_settings(), chat_id=1,
        persona="dude", persona_custom=None,
        latest_user_text="yo", latest_user_name="Matt",
    )
    user_turns = [m for m in built.messages if m["role"] == "user"]
    assert len(user_turns) == 1
    # The single user turn is labeled.
    assert user_turns[0]["content"] == "Matt: yo"


@pytest.mark.asyncio
async def test_memory_disabled_skips_all_history_layers_and_ends_with_user():
    """Passing memory_enabled=False must not consult the store for
    summary/facts/semantic/recent — that's the user-facing semantic AND
    it prevents the embeddings round-trip from churning the OpenAI quota
    in chats that have opted out of memory. The output is just the
    persona system prompt + the current user message."""
    store = FakeStore(recent=[_msg("stale assistant turn", role="assistant")])
    built = await build_context(
        store=store, settings=_settings(), chat_id=1,
        persona="dude", persona_custom=None,
        latest_user_text="hello", latest_user_name="Matt",
        memory_enabled=False,
    )
    # No semantic_search / list_facts / latest_summary call should fire.
    assert store.semantic_calls == 0
    assert store.facts_calls == 0
    assert store.summary_calls == 0
    assert store.recent_calls == 0
    assert built.messages[-1] == {"role": "user", "content": "Matt: hello"}
    # No history bled into the messages array.
    assert all(m.get("content") != "stale assistant turn"
               for m in built.messages)


# ---------------------------- speaker-name labeling --------------------------
@pytest.mark.asyncio
async def test_user_turns_are_prefixed_with_speaker_name():
    """When multiple users have been talking, each of their messages must
    carry a name prefix so the model can tell them apart."""
    store = FakeStore(recent=[
        _msg("hey", role="user", mid=1, author_name="Matt"),
        _msg("hello", role="user", mid=2, author_name="Sarah"),
        _msg("welcome", role="assistant", mid=3),
    ])
    built = await build_context(
        store=store, settings=_settings(), chat_id=1,
        persona="dude", persona_custom=None,
        latest_user_text="who's here", latest_user_name="Matt",
    )
    # Every user-role message now starts with '<Name>: '.
    user_turns = [m for m in built.messages if m["role"] == "user"]
    assert all(":" in m["content"].split(" ", 1)[0]
               or m["content"].split(":", 1)[0].strip()
               for m in user_turns)
    contents = [m["content"] for m in user_turns]
    assert "Matt: hey" in contents
    assert "Sarah: hello" in contents
    # Assistant turns are NOT prefixed (the bot doesn't quote its own name).
    asst_turns = [m for m in built.messages if m["role"] == "assistant"]
    assert all(not c["content"].startswith("Matt:") for c in asst_turns)
    assert all(not c["content"].startswith("Sarah:") for c in asst_turns)


@pytest.mark.asyncio
async def test_user_turn_without_author_name_is_labeled_someone():
    """A user turn with no resolvable author must NOT be left bare — an
    unlabeled turn gets merged into a neighbour's and misattributed. It's
    labeled 'someone:' instead."""
    store = FakeStore(recent=[_msg("legacy line", author_name=None)])
    built = await build_context(
        store=store, settings=_settings(), chat_id=1,
        persona="dude", persona_custom=None,
        latest_user_text="now", latest_user_name=None,
    )
    joined = "\n".join(m["content"] for m in built.messages if m["role"] == "user")
    assert "someone: legacy line" in joined
    assert "someone: now" in joined


@pytest.mark.asyncio
async def test_persona_system_explains_name_prefix_convention():
    """The model needs to know WHY user messages are prefixed so it
    understands the labels are speaker IDs, not part of the message
    content."""
    store = FakeStore(recent=[])
    built = await build_context(
        store=store, settings=_settings(), chat_id=1,
        persona="dude", persona_custom=None,
        latest_user_text="ping", latest_user_name="Matt",
    )
    system_blob = "\n".join(
        m["content"] for m in built.messages if m["role"] == "system"
    )
    # The system prompt set explains the convention to the model.
    assert "display name" in system_blob.lower() or "prefixed" in system_blob.lower()


# --------------------------------- temporal awareness ------------------------
def test_settings_tzinfo_resolves_and_falls_back():
    from datetime import timezone

    from zoneinfo import ZoneInfo

    s = _settings()
    s.bot_timezone = "America/New_York"
    assert s.tzinfo == ZoneInfo("America/New_York")
    # Garbage zone name falls back to UTC instead of raising.
    s.bot_timezone = "Mars/Olympus_Mons"
    assert s.tzinfo == timezone.utc


def test_humanize_span_units():
    from ipedro.memory.context_builder import _humanize_span

    assert _humanize_span(90, suffix=" ago") == "about 2 minutes ago"
    assert _humanize_span(3600, suffix=" later") == "about 1 hour later"
    assert _humanize_span(3 * 3600, suffix=" later") == "about 3 hours later"
    assert _humanize_span(86400, suffix=" ago") == "about 1 day ago"
    assert _humanize_span(3 * 86400, suffix=" later") == "about 3 days later"
    assert _humanize_span(21 * 86400, suffix=" later") == "about 3 weeks later"
    assert _humanize_span(90 * 86400, suffix=" later") == "about 3 months later"
    assert _humanize_span(800 * 86400, suffix=" ago") == "about 2 years ago"


def test_gap_marker_threshold_and_none_handling():
    from datetime import datetime, timezone

    from ipedro.memory.context_builder import _gap_marker

    base = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    tz = timezone.utc
    # < 1h → no marker (normal back-and-forth).
    assert _gap_marker(base, base.replace(minute=30), tz) is None
    # ≥ 1h → marker carries both the exact wall-clock stamp and the
    # humanized span, so the bot has the precise time AND knows how
    # long ago it was at a glance.
    cur = base.replace(hour=15)
    marker = _gap_marker(base, cur, tz)
    assert marker is not None
    assert "⏳" in marker
    assert "later" in marker
    # Exact stamp: short weekday + day + month + year + time.
    assert "2026" in marker and "Jun" in marker and "3:00 PM" in marker
    # Missing timestamps never crash.
    assert _gap_marker(None, base, tz) is None
    assert _gap_marker(base, None, tz) is None


def test_gap_marker_renders_in_the_bot_timezone():
    """A San Diego operator sees Pacific stamps in the marker, not UTC."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from ipedro.memory.context_builder import _gap_marker

    prev = datetime(2026, 6, 21, 0, 0, tzinfo=timezone.utc)
    cur = datetime(2026, 6, 21, 17, 30, tzinfo=timezone.utc)  # 10:30 AM PDT
    marker = _gap_marker(prev, cur, ZoneInfo("America/Los_Angeles"))
    assert marker is not None
    assert "10:30 AM" in marker
    assert "PDT" in marker or "PST" in marker


@pytest.mark.asyncio
async def test_current_time_system_message_is_always_present():
    """Even with memory OFF, the model is told the current time — that's
    situational awareness, not conversation history."""
    from datetime import datetime, timezone

    now = datetime(2026, 6, 21, 14, 47, tzinfo=timezone.utc)
    store = FakeStore(recent=[])
    built = await build_context(
        store=store, settings=_settings(), chat_id=1,
        persona="dude", persona_custom=None,
        latest_user_text="hi", latest_user_name="Matt",
        memory_enabled=False, now=now,
    )
    blob = "\n".join(m["content"] for m in built.messages if m["role"] == "system")
    assert "Right now it is" in blob
    # UTC default → the formatted stamp carries the year.
    assert "2026" in blob


@pytest.mark.asyncio
async def test_long_silence_is_marked_inline_on_the_next_message():
    """A multi-day gap between stored messages surfaces as an inline
    '[⏳ … later]' marker folded into the following message's content, so
    the bot perceives the silence."""
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)

    def at(mins_ago, content, name="Matt", role="user"):
        return StoredMessage(
            id=mins_ago, chat_id=1, message_id=mins_ago, user_id=42,
            role=role, content=content, tokens=None,
            created_at=now - timedelta(minutes=mins_ago),
            author_name=name if role == "user" else None,
        )

    store = FakeStore(recent=[
        at(4320, "night all"),       # 3 days ago
        at(1, "morning, you up?"),   # now-ish
    ])
    built = await build_context(
        store=store, settings=_settings(), chat_id=1,
        persona="dude", persona_custom=None,
        latest_user_text="morning, you up?", latest_user_name="Matt",
        now=now,
    )
    user_turns = [m["content"] for m in built.messages if m["role"] == "user"]
    # The current turn appears exactly once (the marker prefix must not
    # defeat the dedup).
    morning = [c for c in user_turns if c.endswith("morning, you up?")]
    assert len(morning) == 1
    # …and it carries the silence marker.
    assert "⏳" in morning[0]
    assert "day" in morning[0]


@pytest.mark.asyncio
async def test_no_marker_for_rapid_back_and_forth():
    """Messages seconds apart don't get cluttered with time markers."""
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)

    def at(secs_ago, content, name="Matt"):
        return StoredMessage(
            id=secs_ago, chat_id=1, message_id=secs_ago, user_id=42,
            role="user", content=content, tokens=None,
            created_at=now - timedelta(seconds=secs_ago), author_name=name,
        )

    store = FakeStore(recent=[at(20, "yo"), at(10, "you there"), at(2, "hello?")])
    built = await build_context(
        store=store, settings=_settings(), chat_id=1,
        persona="dude", persona_custom=None,
        latest_user_text="hello?", latest_user_name="Matt", now=now,
    )
    # No marker on the actual conversation turns (the system 'current time'
    # note legitimately contains an example marker, so exclude it).
    convo = [m for m in built.messages if m["role"] != "system"]
    assert all("⏳" not in m["content"] for m in convo)
