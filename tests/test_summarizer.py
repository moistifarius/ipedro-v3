"""Rolling-summary maintenance with stubbed store + AI client.

The store and AI client are faked: we care that an empty summary aborts
the whole pass (no facts inserted, covers_until_id untouched — otherwise
the same batch re-triggers forever and re-inserts identical facts), and
that every AI call carries chat_id so /cost attributes the spend.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ipedro.config import Settings
from ipedro.db.repositories import StoredMessage
from ipedro.memory.summarizer import force_summarize, maybe_summarize


class FakeAI:
    """Returns queued responses; records every short_completion call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def short_completion(self, prompt, *, max_tokens=200, chat_id=None):
        self.calls.append({"prompt": prompt, "chat_id": chat_id})
        return self._responses.pop(0) if self._responses else None


class _FakeMessagesRepo:
    def __init__(self, batch):
        self._batch = batch

    async def count_since(self, chat_id, since_id):
        return len(self._batch)

    async def range_for_summary(self, chat_id, since_id, take):
        return self._batch[:take]


class FakeStore:
    def __init__(self, batch):
        self.messages = _FakeMessagesRepo(batch)
        self.summaries_added: list[tuple] = []
        self.facts_added: list[str] = []

    async def latest_summary(self, chat_id):
        return None

    async def add_summary(self, chat_id, summary, covers_until_id):
        self.summaries_added.append((chat_id, summary, covers_until_id))
        return 1

    async def add_fact(self, chat_id, fact, user_id=None, source_msg=None):
        self.facts_added.append(fact)
        return 1


def _msg(mid, content):
    return StoredMessage(
        id=mid, chat_id=1, message_id=mid, user_id=42, role="user",
        content=content, tokens=None,
        created_at=datetime.now(timezone.utc), author_name="Matt",
    )


def _settings():
    return Settings(  # type: ignore[call-arg]
        telegram_bot_token="t", openai_api_key="k",
        database_url="postgresql://t/t",
        summary_trigger_messages=3, summary_keep_recent=1,
    )


def _store():
    # 5 stored messages, keep_recent=1 → the batch summarized is ids 1-4.
    return FakeStore([_msg(i, f"line {i}") for i in range(1, 6)])


@pytest.mark.asyncio
async def test_empty_summary_skips_fact_extraction():
    """When the AI returns nothing for the summary, covers_until_id can't
    advance, so the SAME batch re-triggers on the next message. Extracting
    facts anyway would re-insert identical facts on every pass (the facts
    table has no unique constraint) — the whole pass must abort."""
    store = _store()
    ai = FakeAI([None])  # summary call comes back empty
    await maybe_summarize(store, ai, _settings(), 1)
    assert store.summaries_added == []  # covers logic untouched
    assert store.facts_added == []      # fact repo never touched
    assert len(ai.calls) == 1           # the fact-extraction call never fired


@pytest.mark.asyncio
async def test_summary_and_facts_stored_with_chat_id_attribution():
    store = _store()
    ai = FakeAI(["a tidy summary", "- Matt likes ducks"])
    await maybe_summarize(store, ai, _settings(), 1)
    # Summary covers up to the last message in the batch (id 4).
    assert store.summaries_added == [(1, "a tidy summary", 4)]
    assert store.facts_added == ["Matt likes ducks"]
    # Both AI calls carry chat_id so /cost attributes the spend.
    assert [c["chat_id"] for c in ai.calls] == [1, 1]


@pytest.mark.asyncio
async def test_force_summarize_passes_chat_id_to_all_ai_calls():
    store = _store()
    ai = FakeAI(["forced summary", "NONE"])
    report = await force_summarize(store, ai, _settings(), 1)
    assert report["ok"] is True
    assert store.summaries_added == [(1, "forced summary", 4)]
    assert store.facts_added == []  # "NONE" → nothing durable
    assert [c["chat_id"] for c in ai.calls] == [1, 1]
