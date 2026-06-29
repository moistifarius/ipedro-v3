"""Memory store: writing messages, facts and embeddings.

Wraps the repositories so handlers have a single object to talk to.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

from ipedro.db.pool import Database
from ipedro.db.repositories import (
    EmbeddingRepo, FactRepo, MessageRepo, SummaryRepo, StoredFact,
    StoredMessage, StoredSummary,
)
from ipedro.memory.tokens import count_tokens
from ipedro.openai_client import OpenAIClient

log = logging.getLogger(__name__)


@dataclass
class MemoryStore:
    db: Database
    openai: OpenAIClient | None = None
    pgvector_available: bool = True

    def __post_init__(self) -> None:
        self.messages = MessageRepo(self.db)
        self.summaries = SummaryRepo(self.db)
        self.facts = FactRepo(self.db)
        self.embeddings = EmbeddingRepo(self.db)

    async def record_message(
        self,
        *,
        chat_id: int,
        role: str,
        content: str,
        message_id: int | None = None,
        user_id: int | None = None,
        do_embed: bool = True,
    ) -> int:
        tokens = count_tokens(content)
        msg_id = await self.messages.add(
            chat_id=chat_id, role=role, content=content,
            message_id=message_id, user_id=user_id, tokens=tokens,
        )
        if do_embed and self.openai and self.pgvector_available and content.strip():
            embedding = await self.openai.embed(content)
            if embedding:
                await self.embeddings.upsert(
                    chat_id, "message", msg_id, content[:2000], embedding,
                )
        return msg_id

    async def add_fact(
        self, chat_id: int, fact: str, user_id: int | None = None,
        source_msg: int | None = None,
    ) -> int:
        fid = await self.facts.add(chat_id, fact, user_id, source_msg)
        if self.openai and self.pgvector_available:
            embedding = await self.openai.embed(fact)
            if embedding:
                await self.embeddings.upsert(chat_id, "fact", fid, fact, embedding)
        return fid

    async def add_summary(self, chat_id: int, summary: str, covers_until_id: int) -> int:
        sid = await self.summaries.add(chat_id, summary, covers_until_id)
        if self.openai and self.pgvector_available:
            embedding = await self.openai.embed(summary)
            if embedding:
                await self.embeddings.upsert(chat_id, "summary", sid, summary, embedding)
        return sid

    async def semantic_search(
        self, chat_id: int, query: str, k: int = 6,
    ) -> list[dict]:
        if not self.openai or not self.pgvector_available or not query.strip():
            return []
        embedding = await self.openai.embed(query)
        if not embedding:
            return []
        return await self.embeddings.search(chat_id, embedding, k=k)

    async def recent_messages(self, chat_id: int, limit: int) -> list[StoredMessage]:
        return await self.messages.recent(chat_id, limit)

    async def latest_summary(self, chat_id: int) -> StoredSummary | None:
        return await self.summaries.latest(chat_id)

    async def list_facts(self, chat_id: int, limit: int = 50) -> list[StoredFact]:
        return await self.facts.list_for_chat(chat_id, limit)

    async def delete_fact(self, fact_id: int) -> None:
        await self.facts.delete(fact_id)

    async def wipe_conversation(
        self, chat_id: int, *, include_facts: bool = False,
    ) -> dict[str, int]:
        """Erase a chat's stored conversation so a new persona isn't
        fighting old precedent.

        Deletes, for ``chat_id``:
          * every stored message (both roles — the bot's old-voice
            assistant replies are what the model mimics, and orphaned
            user turns aren't worth keeping),
          * the running summaries (written in/about the old persona),
          * the embeddings (so semantic retrieval can't resurface old
            voice), and
          * optionally the durable facts (off by default — facts are
            usually about *people*, not the bot's voice).

        Returns a dict of {table: rows_deleted}. Idempotent; safe to run
        on a chat with no memory.
        """
        def _count(status: str) -> int:
            # asyncpg returns e.g. "DELETE 42"
            try:
                return int(status.split()[-1])
            except (AttributeError, ValueError, IndexError):
                return 0

        results: dict[str, int] = {}
        # Embeddings first (no FK dependency either way, but tidy).
        results["embeddings"] = _count(await self.db.execute(
            "DELETE FROM embeddings WHERE chat_id = $1", chat_id,
        ))
        results["summaries"] = _count(await self.db.execute(
            "DELETE FROM summaries WHERE chat_id = $1", chat_id,
        ))
        results["messages"] = _count(await self.db.execute(
            "DELETE FROM messages WHERE chat_id = $1", chat_id,
        ))
        if include_facts:
            results["facts"] = _count(await self.db.execute(
                "DELETE FROM facts WHERE chat_id = $1", chat_id,
            ))
        return results

    async def correct_name(
        self, chat_id: int, wrong: str, right: str,
    ) -> dict[str, int]:
        """Recursively fix a mis-attributed name across a chat's DERIVED
        memory layers — the running summaries, durable facts, and the
        bot's own assistant messages — and re-embed whatever changed so
        semantic retrieval reflects the fix.

        Raw *user* messages are deliberately left untouched: their author
        is the ground-truth ``user_id`` from Telegram, and their body is
        what the person literally typed (which may legitimately mention
        the 'wrong' name). The mis-attribution the bot makes lives only in
        the text it generates — that's what we rewrite.

        Whole-word, case-insensitive replace; ``right`` is written exactly
        as given. Returns {layer: rows_changed}.
        """
        results = {"summaries": 0, "facts": 0, "messages": 0}
        if not wrong.strip() or not right.strip():
            return results

        async def _fix(table: str, col: str, ref_kind: str) -> int:
            rows = await self.db.fetch(
                f"SELECT id, {col} AS body FROM {table} WHERE chat_id = $1",
                chat_id,
            )
            changed = 0
            for r in rows:
                body = r["body"] or ""
                new = _replace_whole_word(body, wrong, right)
                if new == body:
                    continue
                await self.db.execute(
                    f"UPDATE {table} SET {col} = $1 WHERE id = $2", new, r["id"],
                )
                await self._reembed(chat_id, ref_kind, r["id"], new)
                changed += 1
            return changed

        results["summaries"] = await _fix("summaries", "summary", "summary")
        results["facts"] = await _fix("facts", "fact", "fact")

        # Assistant messages only (role='assistant'); user turns are sacred.
        rows = await self.db.fetch(
            "SELECT id, content FROM messages "
            " WHERE chat_id = $1 AND role = 'assistant'",
            chat_id,
        )
        for r in rows:
            body = r["content"] or ""
            new = _replace_whole_word(body, wrong, right)
            if new == body:
                continue
            await self.db.execute(
                "UPDATE messages SET content = $1 WHERE id = $2", new, r["id"],
            )
            await self._reembed(chat_id, "message", r["id"], new)
            results["messages"] += 1
        return results

    async def _reembed(
        self, chat_id: int, ref_kind: str, ref_id: int, content: str,
    ) -> None:
        """Recompute and upsert the embedding for a corrected row, so
        semantic search returns the fixed text. No-op when embeddings are
        unavailable."""
        if not (self.openai and self.pgvector_available and content.strip()):
            return
        embedding = await self.openai.embed(content)
        if embedding:
            await self.embeddings.upsert(
                chat_id, ref_kind, ref_id, content[:2000], embedding,
            )


def _replace_whole_word(text: str, wrong: str, right: str) -> str:
    """Case-insensitive whole-word replace of ``wrong`` with ``right``.

    Word boundaries keep 'Matt' from matching inside 'Mattress'. Multi-word
    names ('Big Joe') are supported because re.escape handles the space and
    \\b sits at the outer edges. ``right`` is inserted verbatim.
    """
    if not wrong:
        return text
    pattern = re.compile(rf"\b{re.escape(wrong)}\b", re.IGNORECASE)
    return pattern.sub(lambda _m: right, text)
