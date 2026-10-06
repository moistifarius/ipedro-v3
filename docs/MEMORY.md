# Memory model

Each bot builds AI context from four complementary stores in its own
Postgres database. Everything is per-chat. (Dale's is `ipedro`; every bot
added with `/newbot` has its own, `ipedro_bot_<id>`, on the same server.)

## Layers

1. **Raw messages** (`messages`). Every inbound user message and every
   outbound bot reply is persisted with a token count and a Telegram
   message id (when applicable). This is the source of truth. Lines that
   other bots say in a group arrive through the hub (see below) and are
   stored here too, as `role = 'user'` under the other bot's user id
   (`users.is_bot` is set, and the recap, on-this-day and impersonation
   features leave those out).

2. **Rolling summary** (`summaries`). Once the conversation accumulates
   `SUMMARY_TRIGGER_MESSAGES` new entries beyond the previous summary,
   the older slice (all but the most recent `SUMMARY_KEEP_RECENT`) is
   condensed by the model and stored. The previous summary is folded
   into the new one, so the running narrative compounds rather than
   restarts. One pass at a time per chat, and if the model returns no
   summary the whole pass is skipped, so the same batch is retried later.

3. **Durable facts** (`facts`). During the same summarization pass, the
   model extracts the facts worth keeping ("Alice is a vegetarian", "the
   chat usually meets on Fridays"). There is **no cap per pass**: every
   non-empty line it returns is stored, verbatim, up to 280 characters.
   Lines that read like instructions are skipped by the prompt. A prompt
   shows only the **newest 20** facts, so older ones stay in the table
   but fall out of what the bot sees until the newer ones are cleaned
   up (`/memory_forget`).

4. **Embeddings** (`embeddings`). Each message, summary, and fact is
   embedded with `OPENAI_EMBEDDING_MODEL` (at `OPENAI_EMBEDDING_DIM`
   dimensions; a model that returns another size is refused with one
   clear error). When a new user message arrives, we embed it and pull the
   top-`SEMANTIC_RETRIEVAL_K` cosine-similar memories to inject into the
   prompt. The similarity floor is **0.25 and fixed in code**
   (`memory/context_builder.py`), not an environment setting.

Alongside these, `media_library` keeps the pictures people post (described
by a vision model) so they can be sent back, and `activity_log` keeps the
"why did / didn't it reply" records the bot reads when asked
(`ACTIVITY_RETENTION_DAYS`, 90 by default).

## What members' text is, to the model

Summaries, facts and recalled messages are things people typed, so they are
rendered into the prompt as *notes about the chat*, with a standing line
saying they are not instructions. See [`SECURITY.md`](SECURITY.md).

## Context assembly

When the bot decides to reply, [`memory/context_builder.py`](../ipedro/memory/context_builder.py)
constructs the `messages` array in this priority order, while
respecting `CONTEXT_MAX_TOKENS`:

1. Persona system prompt (`personas.py`).
2. The latest running summary.
3. Durable facts block.
4. Semantically retrieved snippets relevant to the latest user input.
5. The tail of the raw message log (up to `CONTEXT_RECENT_MESSAGES`, in a
   window that is anchored rather than sliding, so it can grow to twice
   that before it re-anchors).

If the token budget runs out, the lowest-priority pieces (recent
messages first) are dropped. The persona goes in first, so it is the last
to go, but it is not exempt: one larger than `CONTEXT_MAX_TOKENS` is left
out and the bot answers without it. That is logged as a `WARNING` naming
the block and the chat (once per chat per process), and `/chat_config
persona ... setfile` and `/master_prompt` warn about it when you set it.

Caching: the stable part of the system prompt (persona, rules, summary,
facts) carries an explicit cache breakpoint; the clock, retrieved snippets
and style reminder come after it. The conversation itself is not cached
(see [`AUDIT.md`](AUDIT.md) for why, and what changing that would take).

## Trade-offs and tuning

- The counts live in `.env` (`CONTEXT_*`, `SUMMARY_*`,
  `SEMANTIC_RETRIEVAL_K`). Defaults err on the side of being cheap.
- Summarization is opportunistic: it runs after every reply, but is a
  no-op until enough new messages accumulate.
- Per-chat memory can be disabled with `/chat_config memory off`. A
  chat with memory off keeps nothing, including pictures.

## Graceful degradation

- Without the `pgvector` Postgres extension, embeddings tables still
  exist (the column type is rewritten to `TEXT`) but semantic search
  returns an empty list. Everything else continues to work.
- OpenAI errors are logged and treated as "no result"; the bot does not
  crash.

## How bots hear each other (the hub)

Telegram never delivers one bot's group messages to another bot, so the
bots share a channel of their own: each publishes what it says in a group to
`bot_posts` in Dale's database, with a NOTIFY, and each LISTENs. A bot that
hears another bot's line in a chat it is in remembers it (`messages`, as
above) and answers only when named or replied to, up to three bot-to-bot
replies deep and one every 20 seconds per chat. `bot_posts` keeps a day.

## Resetting memory for a chat

Use `/memory_wipe [chat_id] [facts]` (bot admins, in a DM): it clears the
messages, summaries, embeddings and picture library for that chat, and the
facts too with `facts`. By hand:

```sql
DELETE FROM embeddings    WHERE chat_id = <id>;
DELETE FROM media_library WHERE chat_id = <id>;
DELETE FROM facts         WHERE chat_id = <id>;
DELETE FROM summaries     WHERE chat_id = <id>;
DELETE FROM messages      WHERE chat_id = <id>;
```

Other bots' lines can reappear after a wipe: they arrive again the next time
a bot speaks in that chat.
