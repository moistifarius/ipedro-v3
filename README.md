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

Full reference in [`docs/COMMANDS.md`](docs/COMMANDS.md). `/help` in any
chat lists everything; bot admins get an extra admin section appended in
a second message.

### Public

| Command | Purpose |
|---|---|
| `/start`, `/help`, `/get_chat_id` | Greeting, help, current chat id |
| `/chat_config [field value]`, `/config` | Show / change this chat's settings (group admins / DM only); `/config` is an inline-keyboard wizard |
| `/a`, `/askai`, `/ask <q>` | One-shot AI answer (no memory write) |
| `/aigen`, `/generate <prompt>` | Generate an image |
| `/aiedit`, `/aivar` | Preserved aliases; image edit/variation not wired to the current SDK |
| `/aitranslate` | Translate a replied-to voice note (Whisper) |
| `/catfact` | Dubious cat fact |
| `/beneficiality` | Score whether the bot would butt in |
| `/whatdid @user` | Confidently summarize what someone's been up to |
| `/tldr [duration]` | Summarize the recent chat (default 24h) |
| `/mood` | Bot's current mood + word of the day |
| `/haiku` | Haiku about the recent chat |
| `/this_or_that A \| B` | Bot picks, dramatically |
| `/echo @user [topic]` | Mimic that user's style |
| `/roast @user`, `/compliment @user` | What it says on the tin |
| `/lyric <line>` | Bot misheard it |
| `/meme top \| bottom` | Generate a meme image |
| `/duckhunt`, `/quackflag` | Spawn / check current duck |
| `/duckstats`, `/duckfriends`, `/duckname <id> <name>`, `/global_leaderboard` | Duckhunt stats and management |
| `/quote`, `/quotes`, `/unquote <id>` | Save / list / delete quotes |
| `/catchphrases @user`, `/lexicon @user` | Their repeated phrases / top words |
| `/heatmap` | Chat activity by hour-of-day |
| `/whoslurking` | Users silent >7 days |
| `/karma` | Chat karma leaderboard (👍/👎 reactions grant/dock) |
| `/remind <duration> <text>` | Schedule a future reminder (e.g. `1h30m`) |
| `/birthday MM-DD`, `/anniversary <name> MM-DD-YYYY`, `/dates` | Track / list chat dates |
| `/poll Q \| A \| B \| …` | Create a poll |
| `/confess <text>` | Anonymous confession (DM only, not admin-only) |

### Mod (chat admin or bot admin)

| Command | Purpose |
|---|---|
| `/shutup @user [duration]`, `/unshutup @user` | Ignore / release a user |
| `/snark_at @user`, `/unsnark @user` | Extra snark toward / release |
| `/ungrudge @user` | Forgive an auto-grudge |
| `/flags` | Active mod flags in this chat |

### Bot admin (DM only)

| Command | Purpose |
|---|---|
| `/list_chat_ids`, `/pick_chat` | Browse known chats (table / picker) |
| `/send_message <chat_id> <text>` | Send a message to a chat as the bot |
| `/logs [N] [filter]` | Tail the program log ring buffer |
| `/cmdlog` | Command audit log from the DB |
| `/cost [chat_id]` | AI spend last 7 days (works for either provider) |
| `/master_prompt show \| set <text> \| setfile \| reset` | Global persona prompt (use `setfile` to upload a `.txt` for prompts >4079 chars) |
| `/ai_provider show \| claude \| openai` | Switch text-completion provider |
| `/ai_model show \| [provider] <model_id>` | Switch text model for the active (or named) provider |
| `/quack_chat`, `/quack_all` | Admin-spawn ducks via picker / in every enabled chat |
| `/duckstats_reset [chat_id] [user_id\|all]` | Wipe a user's (or every user's) duck_stats counters in a chat; leaderboard-style picker if no args |
| `/memory_facts [chat_id]` | Inline picker (or direct lookup) of stored durable facts |
| `/memory_facts_all` | Every stored fact across every known chat |
| `/memory_forget <fact_id>` | Delete a fact |
| `/memory_stats` | Per-chat memory diagnostics: counts, freshness, embedding coverage |
| `/memory_summary` | Show the latest stored summary (picker) |
| `/memory_summarize_now` | Force a summary + fact-extraction pass on a chat (picker) |
| `/memory_search [chat_id] <query>` | Semantic-search the embedding store; shows top hits with similarity scores |
| `/facts_chat` | Legacy alias for the `/memory_facts` picker |
| `/debug_help` | Index of the debug-only commands |
| `/debug_captcha`, `/debug_challenge`, `/debug_trivia`, `/debug_recipe`, `/debug_duck`, `/debug_sharephoto` | Force-trigger flows for testing |

Plus the ambient triggers: `bang`, `bef`, `ignore` resolve an active duck
(`bef` is AI-gated and may be refused — see below); `bad bot` / `bad
dude` as a reply to a bot message deletes that message.

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
