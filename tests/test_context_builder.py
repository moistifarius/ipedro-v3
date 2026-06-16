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
        latest_user_text="what time is it",
    )
    assert built.messages[-1]["role"] == "user"
    assert built.messages[-1]["content"] == "what time is it"


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
async def test_user_turn_without_author_name_falls_through_unlabeled():
    """Old rows that pre-date the JOIN won't have an author_name. They
    should still appear (just unlabeled) — not crash and not get a stray
    colon."""
    store = FakeStore(recent=[_msg("legacy line", author_name=None)])
    built = await build_context(
        store=store, settings=_settings(), chat_id=1,
        persona="dude", persona_custom=None,
        latest_user_text="now", latest_user_name=None,
    )
    contents = [m["content"] for m in built.messages if m["role"] == "user"]
    assert "legacy line" in contents
    assert "now" in contents


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
