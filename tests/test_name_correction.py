"""Tests for recursive name correction (store.correct_name + helper)."""

from __future__ import annotations

import pytest

from ipedro.memory.store import MemoryStore, _replace_whole_word


# ───────────────────────────── whole-word replace ─────────────────────────
def test_replace_whole_word_basic_and_case_insensitive():
    assert _replace_whole_word("Matt said hi", "Matt", "Sarah") == "Sarah said hi"
    # Any case of the wrong name is caught; replacement is verbatim.
    assert _replace_whole_word("matt and MATT", "Matt", "Sarah") == "Sarah and Sarah"


def test_replace_whole_word_respects_boundaries():
    # 'Matt' inside 'Mattress' must NOT be replaced.
    assert _replace_whole_word("a Mattress for Matt", "Matt", "Sarah") == \
        "a Mattress for Sarah"


def test_replace_whole_word_multiword_name():
    assert _replace_whole_word("Big Joe left", "Big Joe", "Liz") == "Liz left"


def test_replace_whole_word_no_match_is_identity():
    assert _replace_whole_word("nobody here", "Matt", "Sarah") == "nobody here"


def test_replace_whole_word_empty_wrong_is_identity():
    assert _replace_whole_word("text", "", "X") == "text"


# ───────────────────────────── store.correct_name ─────────────────────────
class _FakeDB:
    """Minimal async DB double backed by in-memory tables."""
    def __init__(self):
        self.summaries = [{"id": 1, "summary": "Matt planned the trip. Sarah agreed."}]
        self.facts = [
            {"id": 1, "fact": "Matt drinks oat milk"},
            {"id": 2, "fact": "Liz has a dog"},
        ]
        self.messages = [
            {"id": 1, "role": "user", "content": "Matt is the best"},     # user — sacred
            {"id": 2, "role": "assistant", "content": "As Matt said, tacos."},
        ]
        self.updates: list[tuple] = []

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        # The real queries alias the text column AS body for summaries/facts.
        if "FROM summaries" in q:
            return [{"id": r["id"], "body": r["summary"]} for r in self.summaries]
        if "FROM facts" in q:
            return [{"id": r["id"], "body": r["fact"]} for r in self.facts]
        if "FROM messages" in q and "role = 'assistant'" in q:
            return [
                {"id": r["id"], "content": r["content"]}
                for r in self.messages if r["role"] == "assistant"
            ]
        return []

    async def execute(self, query, *args):
        q = " ".join(query.split())
        self.updates.append((q, args))
        # Reflect updates back into the in-memory tables so assertions can
        # read the corrected state.
        if q.startswith("UPDATE summaries SET summary"):
            new, _id = args
            for r in self.summaries:
                if r["id"] == _id:
                    r["summary"] = new
        elif q.startswith("UPDATE facts SET fact"):
            new, _id = args
            for r in self.facts:
                if r["id"] == _id:
                    r["fact"] = new
        elif q.startswith("UPDATE messages SET content"):
            new, _id = args
            for r in self.messages:
                if r["id"] == _id:
                    r["content"] = new
        return "UPDATE 1"

    @property
    def pool(self):
        """Minimal asyncpg-pool shim: transactions route to this same fake."""
        fake = self

        class _Tx:
            async def __aenter__(self): return self
            async def __aexit__(self, *exc): return False

        class _Conn:
            async def execute(self, q, *a): return await fake.execute(q, *a)
            async def fetch(self, q, *a): return await fake.fetch(q, *a)
            def transaction(self): return _Tx()

        class _Acquire:
            async def __aenter__(self): return _Conn()
            async def __aexit__(self, *exc): return False

        class _Pool:
            def acquire(self): return _Acquire()

        return _Pool()


@pytest.mark.asyncio
async def test_correct_name_rewrites_derived_layers_only():
    db = _FakeDB()
    # No openai → re-embed is a no-op (exercises the unavailable path).
    store = MemoryStore(db=db, openai=None, pgvector_available=False)

    counts = await store.correct_name(1, "Matt", "Sarah")

    assert counts == {"summaries": 1, "facts": 1, "messages": 1}
    # Summary + fact + assistant message corrected.
    assert db.summaries[0]["summary"] == "Sarah planned the trip. Sarah agreed."
    assert db.facts[0]["fact"] == "Sarah drinks oat milk"
    assert db.messages[1]["content"] == "As Sarah said, tacos."
    # The USER message is untouched — that's ground truth, never rewritten.
    assert db.messages[0]["content"] == "Matt is the best"


@pytest.mark.asyncio
async def test_correct_name_no_op_when_name_absent():
    db = _FakeDB()
    store = MemoryStore(db=db, openai=None, pgvector_available=False)
    counts = await store.correct_name(1, "Gandalf", "Sarah")
    assert counts == {"summaries": 0, "facts": 0, "messages": 0}
    # Nothing written.
    assert not any(u[0].startswith("UPDATE") for u in db.updates)


@pytest.mark.asyncio
async def test_correct_name_rejects_empty_inputs():
    db = _FakeDB()
    store = MemoryStore(db=db, openai=None, pgvector_available=False)
    assert await store.correct_name(1, "", "Sarah") == \
        {"summaries": 0, "facts": 0, "messages": 0}
    assert await store.correct_name(1, "Matt", "  ") == \
        {"summaries": 0, "facts": 0, "messages": 0}


@pytest.mark.asyncio
async def test_correct_name_reembeds_changed_rows_when_available():
    db = _FakeDB()

    class _FakeOpenAI:
        def __init__(self):
            self.embedded: list[str] = []

        async def embed(self, text):
            self.embedded.append(text)
            return [0.1, 0.2, 0.3]

    class _RecordingEmbeddings:
        def __init__(self):
            self.upserts: list[tuple] = []

        async def upsert(self, chat_id, ref_kind, ref_id, content, embedding):
            self.upserts.append((ref_kind, ref_id, content))

    ai = _FakeOpenAI()
    store = MemoryStore(db=db, openai=ai, pgvector_available=True)
    store.embeddings = _RecordingEmbeddings()

    await store.correct_name(1, "Matt", "Sarah")

    # One re-embed per changed row (summary, fact, assistant msg).
    kinds = sorted(k for k, _id, _c in store.embeddings.upserts)
    assert kinds == ["fact", "message", "summary"]
    # The corrected text is what got embedded.
    assert any("Sarah" in c for _k, _id, c in store.embeddings.upserts)
