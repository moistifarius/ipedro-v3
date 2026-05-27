"""SQL-backed repositories. All DB writes go through these helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from ipedro.db.pool import Database

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- chats/users
@dataclass
class ChatConfig:
    chat_id: int
    response_policy: str
    ambient_probability: float
    persona: str
    persona_custom: str | None
    duckhunt_enabled: bool
    voice_transcribe: bool
    memory_enabled: bool


class ChatRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert_chat(self, chat_id: int, chat_type: str, title: str | None) -> None:
        await self.db.execute(
            """
            INSERT INTO chats (chat_id, type, title)
            VALUES ($1, $2, $3)
            ON CONFLICT (chat_id) DO UPDATE
                SET type = EXCLUDED.type,
                    title = EXCLUDED.title,
                    last_seen = NOW()
            """,
            chat_id, chat_type, title,
        )

    async def list_known(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            "SELECT chat_id, type, title, last_seen FROM chats ORDER BY last_seen DESC"
        )
        return [dict(r) for r in rows]

    async def get_config(self, chat_id: int) -> ChatConfig | None:
        row = await self.db.fetchrow(
            "SELECT * FROM chat_config WHERE chat_id = $1", chat_id
        )
        if not row:
            return None
        return ChatConfig(
            chat_id=row["chat_id"],
            response_policy=row["response_policy"],
            ambient_probability=float(row["ambient_probability"]),
            persona=row["persona"],
            persona_custom=row["persona_custom"],
            duckhunt_enabled=row["duckhunt_enabled"],
            voice_transcribe=row["voice_transcribe"],
            memory_enabled=row["memory_enabled"],
        )

    async def upsert_default_config(
        self,
        chat_id: int,
        response_policy: str,
        ambient_probability: float,
        persona: str,
        duckhunt_enabled: bool,
    ) -> ChatConfig:
        row = await self.db.fetchrow(
            """
            INSERT INTO chat_config (chat_id, response_policy, ambient_probability,
                                     persona, duckhunt_enabled)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (chat_id) DO UPDATE
                SET updated_at = chat_config.updated_at  -- no-op update to return row
            RETURNING *
            """,
            chat_id, response_policy, ambient_probability, persona, duckhunt_enabled,
        )
        assert row is not None
        return ChatConfig(
            chat_id=row["chat_id"],
            response_policy=row["response_policy"],
            ambient_probability=float(row["ambient_probability"]),
            persona=row["persona"],
            persona_custom=row["persona_custom"],
            duckhunt_enabled=row["duckhunt_enabled"],
            voice_transcribe=row["voice_transcribe"],
            memory_enabled=row["memory_enabled"],
        )

    async def update_config(self, chat_id: int, **fields: Any) -> None:
        allowed = {
            "response_policy", "ambient_probability", "persona", "persona_custom",
            "duckhunt_enabled", "voice_transcribe", "memory_enabled",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(updates))
        await self.db.execute(
            f"UPDATE chat_config SET {sets}, updated_at = NOW() WHERE chat_id = $1",
            chat_id, *updates.values(),
        )


class UserRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        is_bot: bool = False,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name, is_bot)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id) DO UPDATE
                SET username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    is_bot = EXCLUDED.is_bot,
                    last_seen = NOW()
            """,
            user_id, username, first_name, last_name, is_bot,
        )


# ---------------------------------------------------------------- messages
@dataclass
class StoredMessage:
    id: int
    chat_id: int
    message_id: int | None
    user_id: int | None
    role: str
    content: str
    tokens: int | None
    created_at: datetime


class MessageRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(
        self,
        chat_id: int,
        role: str,
        content: str,
        *,
        message_id: int | None = None,
        user_id: int | None = None,
        tokens: int | None = None,
    ) -> int:
        # Silently dedupe Telegram messages with the same (chat_id, message_id).
        val = await self.db.fetchval(
            """
            INSERT INTO messages (chat_id, message_id, user_id, role, content, tokens)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (chat_id, message_id) DO UPDATE SET content = messages.content
            RETURNING id
            """,
            chat_id, message_id, user_id, role, content, tokens,
        )
        return int(val)

    async def recent(self, chat_id: int, limit: int) -> list[StoredMessage]:
        rows = await self.db.fetch(
            """
            SELECT id, chat_id, message_id, user_id, role, content, tokens, created_at
              FROM messages
             WHERE chat_id = $1
             ORDER BY id DESC
             LIMIT $2
            """,
            chat_id, limit,
        )
        return [StoredMessage(**dict(r)) for r in reversed(rows)]

    async def count_since(self, chat_id: int, since_id: int) -> int:
        val = await self.db.fetchval(
            "SELECT COUNT(*) FROM messages WHERE chat_id = $1 AND id > $2",
            chat_id, since_id,
        )
        return int(val or 0)

    async def range_for_summary(
        self, chat_id: int, after_id: int, limit: int
    ) -> list[StoredMessage]:
        rows = await self.db.fetch(
            """
            SELECT id, chat_id, message_id, user_id, role, content, tokens, created_at
              FROM messages
             WHERE chat_id = $1 AND id > $2
             ORDER BY id ASC
             LIMIT $3
            """,
            chat_id, after_id, limit,
        )
        return [StoredMessage(**dict(r)) for r in rows]


# ---------------------------------------------------------------- summaries
@dataclass
class StoredSummary:
    id: int
    chat_id: int
    summary: str
    covers_until_id: int
    created_at: datetime


class SummaryRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def latest(self, chat_id: int) -> StoredSummary | None:
        row = await self.db.fetchrow(
            "SELECT * FROM summaries WHERE chat_id = $1 ORDER BY id DESC LIMIT 1",
            chat_id,
        )
        return StoredSummary(**dict(row)) if row else None

    async def add(self, chat_id: int, summary: str, covers_until_id: int) -> int:
        val = await self.db.fetchval(
            "INSERT INTO summaries (chat_id, summary, covers_until_id) "
            "VALUES ($1, $2, $3) RETURNING id",
            chat_id, summary, covers_until_id,
        )
        return int(val)


# ---------------------------------------------------------------- facts
@dataclass
class StoredFact:
    id: int
    chat_id: int
    user_id: int | None
    fact: str
    source_msg: int | None
    created_at: datetime


class FactRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(
        self, chat_id: int, fact: str, user_id: int | None = None,
        source_msg: int | None = None,
    ) -> int:
        val = await self.db.fetchval(
            "INSERT INTO facts (chat_id, user_id, fact, source_msg) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            chat_id, user_id, fact, source_msg,
        )
        return int(val)

    async def list_for_chat(self, chat_id: int, limit: int = 50) -> list[StoredFact]:
        rows = await self.db.fetch(
            "SELECT * FROM facts WHERE chat_id = $1 ORDER BY id DESC LIMIT $2",
            chat_id, limit,
        )
        return [StoredFact(**dict(r)) for r in rows]

    async def delete(self, fact_id: int) -> None:
        await self.db.execute("DELETE FROM facts WHERE id = $1", fact_id)


# ---------------------------------------------------------------- embeddings
class EmbeddingRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(
        self,
        chat_id: int,
        ref_kind: str,
        ref_id: int,
        content: str,
        embedding: Sequence[float] | None,
    ) -> None:
        try:
            await self.db.execute(
                """
                INSERT INTO embeddings (chat_id, ref_kind, ref_id, content, embedding)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (chat_id, ref_kind, ref_id) DO UPDATE
                    SET content = EXCLUDED.content,
                        embedding = EXCLUDED.embedding
                """,
                chat_id, ref_kind, ref_id, content, list(embedding) if embedding else None,
            )
        except Exception as exc:
            log.warning("Embedding upsert failed (ref=%s/%s): %s", ref_kind, ref_id, exc)

    async def search(
        self, chat_id: int, embedding: Sequence[float], k: int = 6
    ) -> list[dict[str, Any]]:
        try:
            rows = await self.db.fetch(
                """
                SELECT ref_kind, ref_id, content,
                       1 - (embedding <=> $2) AS similarity
                  FROM embeddings
                 WHERE chat_id = $1 AND embedding IS NOT NULL
                 ORDER BY embedding <=> $2
                 LIMIT $3
                """,
                chat_id, list(embedding), k,
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("Vector search unavailable: %s", exc)
            return []


# ---------------------------------------------------------------- audit
class CommandLogRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(
        self,
        chat_id: int | None,
        user_id: int | None,
        command: str,
        args: str | None,
        success: bool,
        error: str | None = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO command_log (chat_id, user_id, command, args, success, error)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            chat_id, user_id, command, args, success, error,
        )

    async def tail(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            "SELECT chat_id, user_id, command, args, success, error, created_at "
            "  FROM command_log ORDER BY id DESC LIMIT $1",
            limit,
        )
        return [dict(r) for r in rows]
