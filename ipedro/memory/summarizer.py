"""Rolling-summary maintenance.

After SUMMARY_TRIGGER_MESSAGES new messages have arrived since the last
summary, fold the older portion of the conversation into the running summary
and store any extracted durable facts.
"""

from __future__ import annotations

import asyncio
import logging

from ipedro.config import Settings
from ipedro.db.repositories import StoredMessage
from ipedro.memory.store import MemoryStore
from ipedro.openai_client import OpenAIClient
from ipedro.prompts import FACT_EXTRACT_PROMPT, SUMMARIZE_PROMPT

log = logging.getLogger(__name__)


# Per-chat asyncio locks so two concurrent admin clicks on
# `force_summarize` for the same chat serialize. The dict grows
# unbounded; that's fine for an admin-only feature with <50 chats.
_force_summarize_locks: dict[int, asyncio.Lock] = {}


def _lock_for(chat_id: int) -> asyncio.Lock:
    return _force_summarize_locks.setdefault(chat_id, asyncio.Lock())


def _format_messages_block(messages: list[StoredMessage]) -> str:
    lines = []
    for m in messages:
        who = m.role if m.role != "user" else f"user[{m.user_id}]"
        lines.append(f"{who}: {m.content}")
    return "\n".join(lines)


async def maybe_summarize(
    store: MemoryStore, openai: OpenAIClient, settings: Settings, chat_id: int,
) -> None:
    """Trigger summarization if enough new messages have accumulated."""
    last = await store.latest_summary(chat_id)
    since_id = last.covers_until_id if last else 0
    new_count = await store.messages.count_since(chat_id, since_id)
    if new_count < settings.summary_trigger_messages:
        return

    # Take all-but-the-most-recent N messages so recent context isn't cannibalised.
    take = max(0, new_count - settings.summary_keep_recent)
    if take <= 0:
        return

    batch = await store.messages.range_for_summary(chat_id, since_id, take)
    if not batch:
        return

    msg_block = _format_messages_block(batch)
    summary_text = await openai.short_completion(
        SUMMARIZE_PROMPT.format(
            prior=(last.summary if last else "(none)"),
            messages=msg_block,
        ),
        max_tokens=400,
    )
    if summary_text:
        await store.add_summary(chat_id, summary_text, batch[-1].id)
        log.info("Stored new summary for chat %s covering up to msg %s.", chat_id, batch[-1].id)

    # Extract durable facts in the same pass.
    facts_text = await openai.short_completion(
        FACT_EXTRACT_PROMPT.format(messages=msg_block), max_tokens=200,
    )
    if facts_text and facts_text.strip().upper() != "NONE":
        for line in facts_text.splitlines():
            fact = line.strip().lstrip("-•* ").strip()
            if not fact or fact.upper() == "NONE":
                continue
            if len(fact) > 280:
                fact = fact[:280]
            await store.add_fact(chat_id, fact)


async def force_summarize(
    store: MemoryStore,
    openai: OpenAIClient,
    settings: Settings,
    chat_id: int,
    *,
    keep_recent: int | None = None,
) -> dict:
    """Run summarization + fact extraction *now*, ignoring the message-count
    threshold. Always keeps the last `keep_recent` (default
    `settings.summary_keep_recent`) messages out of the batch so they stay
    in the live context window. Returns a small report dict for the caller.

    Per-chat lock serializes concurrent admin clicks on the same chat.
    """
    async with _lock_for(chat_id):
        keep = settings.summary_keep_recent if keep_recent is None else keep_recent
        last = await store.latest_summary(chat_id)
        since_id = last.covers_until_id if last else 0
        new_count = await store.messages.count_since(chat_id, since_id)
        take = max(0, new_count - keep)
        if take <= 0:
            return {
                "ok": False,
                "reason": (
                    f"only {new_count} new messages since last summary; need "
                    f"more than {keep} to leave anything out of the recent window."
                ),
            }
        batch = await store.messages.range_for_summary(chat_id, since_id, take)
        if not batch:
            return {"ok": False, "reason": "no messages returned for range."}

        msg_block = _format_messages_block(batch)
        summary_text = await openai.short_completion(
            SUMMARIZE_PROMPT.format(
                prior=(last.summary if last else "(none)"),
                messages=msg_block,
            ),
            max_tokens=400,
        )
        new_summary_id: int | None = None
        if summary_text:
            new_summary_id = await store.add_summary(
                chat_id, summary_text, batch[-1].id,
            )
            log.info("Forced summary for chat %s up to msg %s.", chat_id, batch[-1].id)

        facts_added: list[str] = []
        facts_text = await openai.short_completion(
            FACT_EXTRACT_PROMPT.format(messages=msg_block), max_tokens=200,
        )
        if facts_text and facts_text.strip().upper() != "NONE":
            for line in facts_text.splitlines():
                fact = line.strip().lstrip("-•* ").strip()
                if not fact or fact.upper() == "NONE":
                    continue
                if len(fact) > 280:
                    fact = fact[:280]
                await store.add_fact(chat_id, fact)
                facts_added.append(fact)

        return {
            "ok": True,
            "messages_summarized": len(batch),
            "summary_id": new_summary_id,
            "summary_chars": len(summary_text or ""),
            "facts_added": facts_added,
        }
