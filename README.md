# iPedro V2

A configurable, memory-aware Telegram bot.

iPedro V2 is a clean rewrite of the original iPedro: it talks via Claude
(Anthropic) for text completions and the OpenAI SDK for everything else
(embeddings, images, audio), keeps durable per-chat conversation memory in
Postgres (with pgvector for semantic recall), preserves every previous
command, and ships in a Docker container that runs comfortably on Unraid.

## Highlights

- **Hybrid AI stack**: Claude Sonnet 4.6 for text completions (chat,
  `/a`, summaries, the duck personality, etc.); OpenAI for embeddings,
  image generation, and audio (Whisper). The text provider is
  runtime-switchable via `/ai_provider` so you can flip back to OpenAI
  GPT without a redeploy.
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
- **Graceful degradation**: missing `pgvector`, transient OpenAI/Anthropic
  errors, failed transcription, or unreachable Telegram do not crash the
  bot. A missing `ANTHROPIC_API_KEY` auto-falls back to OpenAI text.

## Quick start (local)

```bash
cp .env.example .env       # fill in TELEGRAM_BOT_TOKEN, OPENAI_API_KEY,
                           # ANTHROPIC_API_KEY (optional), DATABASE_URL
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
| `/master_prompt`, `/ai_provider`, `/ai_model` | Admin only — see below |

Plus the ambient triggers from the original: `bang`, `bef`, `ignore`
resolve an active duck (`bef` is AI-gated and may be refused — see below);
`bad bot` / `bad pedro` as a reply to a bot message deletes that message.

## AI providers

Text completions default to Claude **Sonnet 4.6**; embeddings, images,
and audio always go to OpenAI. Both keys live in `.env`:

```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...   # optional — without it, text falls back to OpenAI
```

Runtime knobs (admin DM only, persisted across restarts):

```
/ai_provider show              # which provider is active + both models
/ai_provider claude            # use Claude for text
/ai_provider openai            # use OpenAI GPT for text
/ai_model claude-opus-4-7      # change the active provider's model
/ai_model claude claude-haiku-4-5      # explicitly set Claude's model
/ai_model openai gpt-4.1-mini  # explicitly set OpenAI's model
```

Cost (token counts and a rough USD estimate) is logged per call into the
`openai_usage` table for either provider; `/cost` reports against the
combined log.

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
