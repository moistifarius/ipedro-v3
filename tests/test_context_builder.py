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

    async def recent_messages(self, chat_id, limit):
        return self._recent[-limit:]

    async def latest_summary(self, chat_id):
        return self._summary

    async def list_facts(self, chat_id, limit=50):
        return self._facts[:limit]

    async def semantic_search(self, chat_id, query, k=6):
        return self._semantic


def _msg(content, role="user", mid=1):
    return StoredMessage(
        id=mid, chat_id=1, message_id=mid, user_id=42, role=role,
        content=content, tokens=None, created_at=datetime.now(timezone.utc),
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
