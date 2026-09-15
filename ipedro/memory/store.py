"""Memory store: writing messages, facts and embeddings.

Wraps the repositories so handlers have a single object to talk to.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

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
        # One transaction: a failure partway must not leave e.g. embeddings
        # wiped while the messages they point at survive.
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                results["embeddings"] = _count(await conn.execute(
                    "DELETE FROM embeddings WHERE chat_id = $1", chat_id,
                ))
                results["summaries"] = _count(await conn.execute(
                    "DELETE FROM summaries WHERE chat_id = $1", chat_id,
                ))
                results["messages"] = _count(await conn.execute(
                    "DELETE FROM messages WHERE chat_id = $1", chat_id,
                ))
                results["media_library"] = _count(await conn.execute(
                    "DELETE FROM media_library WHERE chat_id = $1", chat_id,
                ))
                if include_facts:
                    results["facts"] = _count(await conn.execute(
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

        # All text rewrites happen in ONE transaction — a failure halfway must
        # not leave summaries renamed but facts/messages not. Re-embedding
        # happens after commit: embeddings are recoverable, the text isn't,
        # and an OpenAI call doesn't belong inside a DB transaction.
        reembed_jobs: list[tuple[str, int, str]] = []   # (ref_kind, id, text)

        async with self.db.pool.acquire() as conn:
            async with conn.transaction():

                async def _fix(table: str, col: str, ref_kind: str) -> int:
                    rows = await conn.fetch(
                        f"SELECT id, {col} AS body FROM {table} "
                        f"WHERE chat_id = $1",
                        chat_id,
                    )
                    changed = 0
                    for r in rows:
                        body = r["body"] or ""
                        new = _replace_whole_word(body, wrong, right)
                        if new == body:
                            continue
                        await conn.execute(
                            f"UPDATE {table} SET {col} = $1 WHERE id = $2",
                            new, r["id"],
                        )
                        # Delete the now-stale embedding IN the same
                        # transaction, rather than leaving the old
                        # (wrong-name) text searchable until the async
                        # re-embed loop below gets to it. A row briefly
                        # absent from search is fine; one briefly
                        # returning the wrong text is the bug.
                        await conn.execute(
                            "DELETE FROM embeddings WHERE chat_id = $1 "
                            "  AND ref_kind = $2 AND ref_id = $3",
                            chat_id, ref_kind, r["id"],
                        )
                        reembed_jobs.append((ref_kind, r["id"], new))
                        changed += 1
                    return changed

                results["summaries"] = await _fix("summaries", "summary", "summary")
                results["facts"] = await _fix("facts", "fact", "fact")

                # Assistant messages only; user turns are sacred.
                rows = await conn.fetch(
                    "SELECT id, content FROM messages "
                    " WHERE chat_id = $1 AND role = 'assistant'",
                    chat_id,
                )
                for r in rows:
                    body = r["content"] or ""
                    new = _replace_whole_word(body, wrong, right)
                    if new == body:
                        continue
                    await conn.execute(
                        "UPDATE messages SET content = $1 WHERE id = $2",
                        new, r["id"],
                    )
                    await conn.execute(
                        "DELETE FROM embeddings WHERE chat_id = $1 "
                        "  AND ref_kind = 'message' AND ref_id = $2",
                        chat_id, r["id"],
                    )
                    reembed_jobs.append(("message", r["id"], new))
                    results["messages"] += 1

        for ref_kind, ref_id, new_text in reembed_jobs:
            await self._reembed(chat_id, ref_kind, ref_id, new_text)
        return results

    async def unlearn(self, chat_id: int, belief: str) -> dict[str, int]:
        """Scrub a belief the chat planted, across every derived layer.

        The shape of the problem: people tell the bot something false
        ("every photo is Michael"), the fact extractor files it as
        durable, the summarizer folds it into the running summary — which
        every later summary is built on, so it never ages out — and the
        bot's own replies agreeing with it sit in history and retrieval.

        Same rule as correct_name: user messages are never touched. What
        gets scrubbed is what the bot derived — facts that state or lean
        on the belief, the summary (rewritten with the belief removed),
        and the bot's own messages that assert it — judged by the cheap
        model rather than keyword-matched, so "Michael drinks IPA" stays
        while "every photo is Michael" goes. A correction fact is then
        added so the residue in raw history can't re-seed it.

        Returns {facts, summary, messages, correction} row counts.
        """
        results = {"facts": 0, "summary": 0, "messages": 0, "correction": 0}
        belief = " ".join(belief.split())
        if not belief or not self.openai:
            return results

        async def _judge(kind: str, items: list[tuple[int, str]]) -> list[int]:
            """Which numbered items state, imply or depend on the belief."""
            if not items:
                return []
            listing = "\n".join(f"{i + 1}. {text[:300]}" for i, (_, text) in enumerate(items))
            answer = await self.openai.cheap_completion(
                f"A chat bot was tricked into believing something false:\n"
                f"  \"{belief}\"\n\n"
                f"Below are {kind}. Which of them state that belief, imply it, "
                f"repeat a variant of it about the same subject, or only make "
                f"sense if it were true? Ordinary facts that merely mention the "
                f"same people are NOT included.\n\n{listing}\n\n"
                f"Reply with the numbers, comma-separated, or NONE.",
                max_tokens=60, temperature=0.0, chat_id=chat_id,
            )
            picked = {int(n) for n in re.findall(r"\d+", answer or "")}
            return [items[n - 1][0] for n in sorted(picked) if 1 <= n <= len(items)]

        # Read + judge first: the model calls stay out of the transaction.
        fact_rows = await self.db.fetch(
            "SELECT id, fact FROM facts WHERE chat_id = $1 ORDER BY id", chat_id,
        )
        doomed_facts = await _judge(
            "durable facts the bot has stored",
            [(r["id"], r["fact"] or "") for r in fact_rows],
        )

        latest = await self.latest_summary(chat_id)
        new_summary = None
        if latest and latest.summary.strip():
            rewritten = await self.openai.cheap_completion(
                f"Rewrite the summary below with every trace of this false belief "
                f"removed — the belief itself and anything that relies on it:\n"
                f"  \"{belief}\"\n"
                f"Keep everything else exactly as it is, same format. Output only "
                f"the rewritten summary.\n\n{latest.summary}",
                max_tokens=600, temperature=0.0, chat_id=chat_id,
            )
            if rewritten and rewritten.strip() and rewritten.strip() != latest.summary.strip():
                new_summary = rewritten.strip()

        # Candidates among the bot's own messages: the most recent ones no
        # matter what they say — the damage is almost always in the last
        # few turns, phrased any old way ("that's him again") — plus older
        # ones that mention the belief's words. POSIX regex (~*), not
        # ILIKE: ILIKE has no alternation, and "%(a|b)%" is a literal.
        recent_rows = await self.db.fetch(
            "SELECT id, content FROM messages "
            " WHERE chat_id = $1 AND role = 'assistant' "
            " ORDER BY id DESC LIMIT 30",
            chat_id,
        )
        words = [
            w.replace("'", "") for w in re.findall(r"[a-z0-9']+", belief.lower())
            if len(w.replace("'", "")) >= 4
        ]
        hit_rows = []
        if words:
            # "michaels" should also catch "Michael's".
            pattern = "|".join(
                re.sub(r"s$", "'?s", re.escape(w)) for w in words
            )
            hit_rows = await self.db.fetch(
                "SELECT id, content FROM messages "
                " WHERE chat_id = $1 AND role = 'assistant' AND content ~* $2 "
                " ORDER BY id DESC LIMIT 40",
                chat_id, pattern,
            )
        seen: set[int] = set()
        message_rows = []
        for r in list(recent_rows) + list(hit_rows):
            if r["id"] not in seen:
                seen.add(r["id"])
                message_rows.append(r)
        doomed_messages = await _judge(
            "the bot's own past messages",
            [(r["id"], r["content"] or "") for r in message_rows],
        )

        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                if doomed_facts:
                    await conn.execute(
                        "DELETE FROM embeddings WHERE chat_id = $1 "
                        "  AND ref_kind = 'fact' AND ref_id = ANY($2)",
                        chat_id, doomed_facts,
                    )
                    await conn.execute(
                        "DELETE FROM facts WHERE id = ANY($1)", doomed_facts,
                    )
                    results["facts"] = len(doomed_facts)
                if new_summary is not None and latest is not None:
                    await conn.execute(
                        "UPDATE summaries SET summary = $1 WHERE id = $2",
                        new_summary, latest.id,
                    )
                    results["summary"] = 1
                if doomed_messages:
                    await conn.execute(
                        "DELETE FROM embeddings WHERE chat_id = $1 "
                        "  AND ref_kind = 'message' AND ref_id = ANY($2)",
                        chat_id, doomed_messages,
                    )
                    await conn.execute(
                        "DELETE FROM messages WHERE id = ANY($1)", doomed_messages,
                    )
                    results["messages"] = len(doomed_messages)

        if new_summary is not None and latest is not None:
            await self._reembed(chat_id, "summary", latest.id, new_summary)
        # The inoculation: raw user lines asserting the belief still exist
        # (they're what people typed), so leave a standing correction where
        # every reply will see it.
        await self.add_fact(
            chat_id,
            f"NOT TRUE, do not repeat: \"{belief}\". The chat was winding you "
            f"up. When someone shows you a picture, say what is actually in it.",
        )
        results["correction"] = 1
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
