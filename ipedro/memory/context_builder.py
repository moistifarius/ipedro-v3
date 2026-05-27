"""Build the AI chat-completion `messages` array for a given chat.

Combines, in priority order:
  1. The persona system prompt.
  2. A condensed running summary (if any).
  3. Durable per-chat facts (if any).
  4. Semantically retrieved older snippets relevant to the latest user query.
  5. Recent raw messages (the tail of the conversation).

Everything is token-budgeted so we never blow past CONTEXT_MAX_TOKENS.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ipedro.config import Settings
from ipedro.db.repositories import StoredMessage
from ipedro.memory.store import MemoryStore
from ipedro.memory.tokens import count_tokens
from ipedro.personas import resolve_persona

log = logging.getLogger(__name__)


@dataclass
class BuiltContext:
    messages: list[dict[str, Any]]
    tokens: int


def _role_for(stored: StoredMessage) -> str:
    if stored.role in ("user", "assistant", "system"):
        return stored.role
    return "user"


async def build_context(
    *,
    store: MemoryStore,
    settings: Settings,
    chat_id: int,
    persona: str,
    persona_custom: str | None,
    latest_user_text: str,
    extra_system: str | None = None,
) -> BuiltContext:
    budget = settings.context_max_tokens
    messages: list[dict[str, Any]] = []
    used = 0

    def _add(msg: dict[str, Any]) -> bool:
        nonlocal used
        cost = count_tokens(msg.get("content", ""))
        if used + cost > budget:
            return False
        messages.append(msg)
        used += cost
        return True

    # 1. Persona
    persona_text = resolve_persona(persona, persona_custom)
    _add({"role": "system", "content": persona_text})
    if extra_system:
        _add({"role": "system", "content": extra_system})

    # 2. Running summary
    summary = await store.latest_summary(chat_id)
    if summary:
        _add({
            "role": "system",
            "content": f"Conversation summary so far:\n{summary.summary}",
        })

    # 3. Durable facts (compact)
    facts = await store.list_facts(chat_id, limit=20)
    if facts:
        fact_block = "Known durable facts about this chat:\n" + "\n".join(
            f"- {f.fact}" for f in facts
        )
        _add({"role": "system", "content": fact_block})

    # 4. Semantic retrieval against the latest user input
    if latest_user_text.strip():
        hits = await store.semantic_search(
            chat_id, latest_user_text, k=settings.semantic_retrieval_k,
        )
        # Keep only meaningful similarity hits.
        hits = [h for h in hits if h.get("similarity", 0) >= 0.25]
        if hits:
            retrieved = "Potentially relevant prior context:\n" + "\n".join(
                f"- ({h['ref_kind']}) {h['content'][:300]}" for h in hits
            )
            _add({"role": "system", "content": retrieved})

    # 5. Recent raw messages (chronological, last N).
    recent = await store.recent_messages(chat_id, settings.context_recent_messages)
    for m in recent:
        if not _add({"role": _role_for(m), "content": m.content}):
            break

    return BuiltContext(messages=messages, tokens=used)
