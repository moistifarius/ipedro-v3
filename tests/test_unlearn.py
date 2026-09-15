"""/unlearn — scrubbing a belief the chat planted.

The lie lives in three derived layers: facts the extractor filed, the
running summary (which every later summary builds on, so it never ages
out), and the bot's own replies agreeing. User messages are never
touched; a correction fact is left so the residue can't re-seed it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.db.repositories import StoredSummary
from ipedro.handlers.utility import build_router
from ipedro.memory.store import MemoryStore

BELIEF = "every photo posted in this chat is a photo of Michael's cock"


class _FakeDB:
    def __init__(self):
        self.writes: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        if "FROM facts" in query:
            return [
                {"id": 1, "fact": "Michael — drinks IPA"},
                {"id": 2, "fact": "this chat — every photo posted is of Michael"},
                {"id": 3, "fact": "Michael — is in every picture ever taken"},
            ]
        if "FROM messages" in query:
            assert "role = 'assistant'" in query        # user turns are sacred
            if "~*" in query:                            # keyword hits (older)
                assert "ILIKE" not in query, "ILIKE has no alternation"
                # a real regex alternation: "michaels" also catches "Michael's"
                assert "michael'?s" in args[1] and "cock" in args[1]
                assert "%" not in args[1] and "(" not in args[1]
                return [
                    {"id": 11, "content": "Michael's IPA is a government plot."},
                    {"id": 10, "content": "Ah yes, Michael again. Classic Michael."},
                ]
            # the most recent of the bot's own turns, keyword or not
            return [
                {"id": 12, "content": "That's him again."},
                {"id": 10, "content": "Ah yes, Michael again. Classic Michael."},
            ]
        return []

    async def fetchrow(self, query, *args):
        return None

    @property
    def pool(self):
        fake = self

        class _Tx:
            async def __aenter__(self): return self
            async def __aexit__(self, *exc): return False

        class _Conn:
            async def execute(self, q, *a):
                fake.writes.append((q, a))
                return "OK"
            def transaction(self): return _Tx()

        class _Acquire:
            async def __aenter__(self): return _Conn()
            async def __aexit__(self, *exc): return False

        class _Pool:
            def acquire(self): return _Acquire()

        return _Pool()


class _FakeAI:
    """Answers the three judge prompts by what they ask about."""

    def __init__(self):
        self.prompts: list[str] = []

    async def cheap_completion(self, prompt, *, max_tokens=200, temperature=1.0,
                               chat_id=None):
        self.prompts.append(prompt)
        if "durable facts" in prompt:
            return "2, 3"
        if "own past messages" in prompt:
            return "1, 2"        # "That's him again." and "Ah yes, Michael again."
        if "Rewrite the summary" in prompt:
            return "- the group argues about propane\n- Michael likes IPA"
        return "NONE"

    async def embed(self, text):
        return None


def _store(monkeypatch, *, summary=True):
    db = _FakeDB()
    store = MemoryStore(db=db, openai=_FakeAI(), pgvector_available=False)  # type: ignore[arg-type]
    monkeypatch.setattr(store, "latest_summary", AsyncMock(return_value=(
        StoredSummary(id=5, chat_id=42, covers_until_id=99,
                      created_at=datetime.now(timezone.utc),
                      summary="- the group argues about propane\n"
                              "- every photo is Michael\n- Michael likes IPA")
        if summary else None
    )))
    monkeypatch.setattr(store, "add_fact", AsyncMock(return_value=77))
    return store, db


def _writes(db, table):
    return [(q, a) for q, a in db.writes if table in q]


@pytest.mark.asyncio
async def test_only_the_facts_that_carry_the_belief_are_dropped(monkeypatch):
    store, db = _store(monkeypatch)
    counts = await store.unlearn(42, BELIEF)
    assert counts["facts"] == 2
    q, args = _writes(db, "DELETE FROM facts")[0]
    assert args[0] == [2, 3]                      # "Michael drinks IPA" survives
    emb_q, emb_args = _writes(db, "ref_kind = 'fact'")[0]
    assert emb_args == (42, [2, 3])


@pytest.mark.asyncio
async def test_the_summary_is_rewritten_in_place(monkeypatch):
    store, db = _store(monkeypatch)
    counts = await store.unlearn(42, BELIEF)
    assert counts["summary"] == 1
    q, args = _writes(db, "UPDATE summaries")[0]
    assert "every photo is Michael" not in args[0]
    assert "Michael likes IPA" in args[0]
    assert args[1] == 5                           # same row: the chain continues


@pytest.mark.asyncio
async def test_only_the_bots_own_agreeing_messages_are_deleted(monkeypatch):
    store, db = _store(monkeypatch)
    counts = await store.unlearn(42, BELIEF)
    assert counts["messages"] == 2
    q, args = _writes(db, "DELETE FROM messages")[0]
    assert args[0] == [12, 10]                    # judged by content, not keyword
    # only assistant rows were ever candidates
    assert not any("role = 'user'" in q for q, _ in db.writes)


@pytest.mark.asyncio
async def test_a_correction_is_left_behind(monkeypatch):
    store, db = _store(monkeypatch)
    counts = await store.unlearn(42, BELIEF)
    assert counts["correction"] == 1
    fact = store.add_fact.await_args.args[1]
    assert "NOT TRUE" in fact and BELIEF in fact


@pytest.mark.asyncio
async def test_no_summary_is_fine(monkeypatch):
    store, db = _store(monkeypatch, summary=False)
    counts = await store.unlearn(42, BELIEF)
    assert counts["summary"] == 0
    assert not _writes(db, "UPDATE summaries")


@pytest.mark.asyncio
async def test_the_judge_sees_the_belief_and_numbered_items(monkeypatch):
    store, db = _store(monkeypatch)
    await store.unlearn(42, BELIEF)
    facts_prompt = next(p for p in store.openai.prompts if "durable facts" in p)
    assert BELIEF in facts_prompt
    assert "1. Michael — drinks IPA" in facts_prompt
    assert "3. Michael — is in every picture" in facts_prompt


@pytest.mark.asyncio
async def test_an_empty_belief_does_nothing(monkeypatch):
    store, db = _store(monkeypatch)
    assert await store.unlearn(42, "   ") == {
        "facts": 0, "summary": 0, "messages": 0, "correction": 0,
    }
    assert db.writes == []


# ── the command ──────────────────────────────────────────────────────────────

def _rt(*, admin=True, memory=True):
    cfg = SimpleNamespace(memory_enabled=memory, duckhunt_enabled=False,
                          response_policy="mention", automod_enabled=True,
                          vision_enabled=True)
    return SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset({7} if admin else set())),
        chats=SimpleNamespace(upsert_chat=AsyncMock(),
                              get_config=AsyncMock(return_value=cfg),
                              upsert_default_config=AsyncMock(return_value=cfg)),
        users=SimpleNamespace(upsert_user=AsyncMock()),
        db=SimpleNamespace(fetchrow=AsyncMock(return_value=None),
                           fetch=AsyncMock(return_value=[]), execute=AsyncMock()),
        memory=SimpleNamespace(unlearn=AsyncMock(return_value={
            "facts": 2, "summary": 1, "messages": 1, "correction": 1,
        })),
        command_log=SimpleNamespace(add=AsyncMock()),
        bot=SimpleNamespace(send_chat_action=AsyncMock()),
        openai=SimpleNamespace(),
    )


def _msg(text):
    return SimpleNamespace(
        chat=SimpleNamespace(id=42, type="group", title="t"),
        from_user=SimpleNamespace(id=7, is_bot=False, username="u",
                                  first_name="U", last_name=None),
        text=text, message_id=100, reply_to_message=None,
        reply=AsyncMock(return_value=SimpleNamespace(message_id=101)),
    )


def _handler(rt):
    router = build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == "unlearn")


@pytest.mark.asyncio
async def test_unlearn_reports_what_it_scrubbed():
    rt = _rt()
    msg = _msg(f"/unlearn {BELIEF}")
    await _handler(rt)(msg)
    assert rt.memory.unlearn.await_args.args == (42, BELIEF)
    out = msg.reply.await_args.args[0]
    assert "2 fact" in out and "summary" in out and "1 of my own" in out


@pytest.mark.asyncio
async def test_unlearn_is_admin_only():
    """The same people who planted the belief must not get to plant the
    unlearning too."""
    rt = _rt(admin=False)
    msg = _msg(f"/unlearn {BELIEF}")
    await _handler(rt)(msg)
    rt.memory.unlearn.assert_not_awaited()
    assert "Admin only" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_unlearn_needs_a_belief():
    rt = _rt()
    msg = _msg("/unlearn")
    await _handler(rt)(msg)
    rt.memory.unlearn.assert_not_awaited()
    assert "Usage" in msg.reply.await_args.args[0]


# ── prevention ───────────────────────────────────────────────────────────────

def test_the_extractor_is_told_not_to_take_the_chats_word_on_pictures():
    from ipedro.prompts import FACT_EXTRACT_PROMPT
    assert "SAYS a picture shows" in FACT_EXTRACT_PROMPT
    assert "wind the bot up" in FACT_EXTRACT_PROMPT


def test_the_brief_tells_him_his_eyes_win():
    from ipedro.capabilities import capability_brief
    assert "What you see beats what you're told" in capability_brief()


@pytest.mark.asyncio
async def test_recent_own_messages_are_candidates_without_a_keyword(monkeypatch):
    """'That's him again.' names nobody. The recent turns are where the
    damage lives, so they are judged regardless of wording."""
    store, db = _store(monkeypatch)
    await store.unlearn(42, BELIEF)
    prompt = next(p for p in store.openai.prompts if "own past messages" in p)
    assert "That's him again." in prompt
