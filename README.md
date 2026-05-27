# iPedro V2

A configurable, memory-aware Telegram bot.

iPedro V2 is a clean rewrite of the original iPedro: it talks via the OpenAI
SDK, keeps durable per-chat conversation memory in Postgres (with pgvector
for semantic recall), preserves every previous command, and ships in a
Docker container that runs comfortably on Unraid.

## Highlights

- **Modern OpenAI Python SDK** (text, embeddings, image, audio).
- **Postgres + pgvector** memory: raw messages, rolling summaries, durable
  facts, embeddings — assembled on demand and token-budgeted.
- **Per-chat config**: response policy, persona, ambient probability,
  duckhunt toggle, voice transcription toggle, memory toggle.
- **Improved Duckhunt** with rarity tiers (common → legendary), points,
  streaks, miss tracking, leaderboards, per-user cooldowns, AI-gated
  `bef` (the duck personality decides whether to be your friend), and a
  retry-challenge mechanic. State is restart-safe.
- **Admin gating** locked to Telegram user id `315660812` and **private DM
  only** — sensitive commands cannot be invoked from groups.
- **Graceful degradation**: missing `pgvector`, transient OpenAI errors,
  failed transcription, or unreachable Telegram do not crash the bot.

## Quick start (local)

```bash
cp .env.example .env       # fill in TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, DATABASE_URL
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m ipedro
```

A running Postgres with the `vector` extension is required for full memory.
Without it, the bot still works — semantic retrieval is disabled and a
warning is logged.

## Docker / Unraid

See [`docs/UNRAID.md`](docs/UNRAID.md). TL;DR:

```bash
cp .env.example .env
# fill in TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, etc.
cd docker && docker compose up -d --build
```

## Commands

Full reference in [`docs/COMMANDS.md`](docs/COMMANDS.md).

| Command | Purpose |
|---|---|
| `/start`, `/help` | Hello / list of commands |
| `/a`, `/askai`, `/ask` | Quick one-shot answer (no memory write) |
| `/aigen <prompt>` | Generate an image |
| `/aiedit`, `/aivar` | Preserved aliases; require a backend SDK feature not yet wired |
| `/aitranslate` | Translate a replied-to voice note (Whisper) |
| `/catfact` | Dubious cat fact |
| `/beneficiality` | Score whether the bot would butt in |
| `/duckhunt` | Force-spawn a duck (if duckhunt is enabled) |
| `/duckstats` | Per-chat leaderboard |
| `/quackflag` | Current duck status |
| `/chat_config` | Show/update per-chat settings (group admins / DM only) |
| `/get_chat_id` | Show this chat id |
| `/list_chat_ids` | Admin only |
| `/send_message` | Admin only |
| `/logs` | Admin only |
| `/memory_facts`, `/memory_forget` | Admin only |

Plus the ambient triggers from the original: `bang`, `bef`, `ignore`
resolve an active duck (`bef` is AI-gated and may be refused — see below);
`bad bot` / `bad pedro` as a reply to a bot message deletes that message.

## Memory model

See [`docs/MEMORY.md`](docs/MEMORY.md). In short: every inbound and outbound
message is persisted; older messages get folded into a rolling summary;
high-signal facts get pulled out and stored separately; everything is
embedded for semantic recall; prompt assembly picks the most useful slices
within a token budget.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests cover admin auth, chat policy, duckhunt scoring, context building,
token counting, persona resolution, and the OpenAI client wrapper. None of
them hit Postgres, Telegram, or OpenAI for real.

## Security

- `.env` is gitignored. Never commit your tokens.
- Admin gating uses **numeric Telegram user id only** — never usernames.
- Admin commands are private-DM only.
- `logging_setup.py` redacts plausible secrets from log lines.

If you previously stored secrets in `agents.md` or anywhere in this repo's
git history, **rotate them** — they are considered leaked.

## Legacy import

To pull stats/messages from a V1 deployment, see
[`scripts/migrate_legacy.py`](scripts/migrate_legacy.py).
