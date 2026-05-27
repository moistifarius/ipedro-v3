"""Memory store: writing messages, facts and embeddings.

Wraps the repositories so handlers have a single object to talk to.
"""

from __future__ import annotations

import logging
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
