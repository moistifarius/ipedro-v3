# Memory model

iPedro V2 builds AI context from four complementary stores, each in
Postgres. Everything is per-chat.

## Layers

1. **Raw messages** (`messages`). Every inbound user message and every
   outbound bot reply is persisted with a token count and a Telegram
   message id (when applicable). This is the source of truth.

2. **Rolling summary** (`summaries`). Once the conversation accumulates
   `SUMMARY_TRIGGER_MESSAGES` new entries beyond the previous summary,
   the older slice (all but the most recent `SUMMARY_KEEP_RECENT`) is
   condensed by the model and stored. The previous summary is folded
   into the new one, so the running narrative compounds rather than
   restarts.

3. **Durable facts** (`facts`). During the same summarization pass, the
   model extracts up to three high-signal facts ("Alice is a
   vegetarian", "the chat usually meets on Fridays"). These are stored
   verbatim and shown to future prompts in full.

4. **Embeddings** (`embeddings`). Each message, summary, and fact is
   embedded with `OPENAI_EMBEDDING_MODEL`. When a new user message
   arrives, we embed it and pull the top-k cosine-similar memories
   (threshold ≥ 0.25) to inject into the prompt.

## Context assembly

When the bot decides to reply, [`memory/context_builder.py`](../ipedro/memory/context_builder.py)
constructs the `messages` array in this priority order, while
respecting `CONTEXT_MAX_TOKENS`:

1. Persona system prompt (`personas.py`).
2. The latest running summary.
3. Durable facts block.
4. Semantically retrieved snippets relevant to the latest user input.
5. The tail of the raw message log (up to `CONTEXT_RECENT_MESSAGES`).

If the token budget runs out, the lowest-priority pieces (recent
messages first) are dropped. Persona is always kept.

## Trade-offs and tuning

- The thresholds and counts live in `.env` (`CONTEXT_*`, `SUMMARY_*`,
  `SEMANTIC_RETRIEVAL_K`). Defaults err on the side of being cheap.
- Summarization is opportunistic: it runs after every reply, but is a
  no-op until enough new messages accumulate.
- Per-chat memory can be disabled with `/chat_config memory off`.

## Graceful degradation

- Without the `pgvector` Postgres extension, embeddings tables still
  exist (the column type is rewritten to `TEXT`) but semantic search
  returns an empty list. Everything else continues to work.
- OpenAI errors are logged and treated as "no result"; the bot does not
  crash.

## Resetting memory for a chat

```sql
DELETE FROM embeddings WHERE chat_id = <id>;
DELETE FROM facts      WHERE chat_id = <id>;
DELETE FROM summaries  WHERE chat_id = <id>;
DELETE FROM messages   WHERE chat_id = <id>;
```
