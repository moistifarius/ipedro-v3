"""Rolling-summary maintenance.

After SUMMARY_TRIGGER_MESSAGES new messages have arrived since the last
summary, fold the older portion of the conversation into the running summary
and store any extracted durable facts.
"""

from __future__ import annotations

import logging

from ipedro.config import Settings
from ipedro.db.repositories import StoredMessage
from ipedro.memory.store import MemoryStore
from ipedro.openai_client import OpenAIClient
from ipedro.prompts import FACT_EXTRACT_PROMPT, SUMMARIZE_PROMPT

log = logging.getLogger(__name__)


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
