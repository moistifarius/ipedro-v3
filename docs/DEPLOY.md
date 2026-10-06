# Generic deployment notes

For Unraid-specific instructions see [`UNRAID.md`](UNRAID.md).

## Required environment

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | From `@BotFather` |
| `OPENAI_API_KEY` | yes | Used for embeddings, image gen, and Whisper audio |
| `ANTHROPIC_API_KEY` | recommended | Used for text completions (Claude). Absent → bot auto-falls back to OpenAI text. |
| `DATABASE_URL` | yes | `postgresql://user:pass@host:port/db` |
| `ADMIN_USER_IDS` | no | `315660812` is always implicitly included |
| `TEXT_PROVIDER` | no | `claude` (default when Anthropic key present) or `openai`. Persisted at runtime via `/ai_provider`. |
| `CLAUDE_TEXT_MODEL` | no | Default `claude-sonnet-5`. Runtime-tunable via `/ai_model`. |
| `OPENAI_TEXT_MODEL` | no | Default `gpt-4o-mini`. Runtime-tunable via `/ai_model`. |
| `OPENAI_*_MODEL` | no | Image / embedding / transcription model overrides |
| `EVOLVE_GITHUB_TOKEN` | no | Fine-grained PAT, this repo only, Issues read/write and nothing else. Lets the owner's `/evolve` file change requests. Unset → `/evolve` says what's missing. |
| `BOT_NAME` | no | Default `Dale`. Set these four to run the same code as a different bot. |
| `BOT_ALIASES` | no | Comma-separated names it answers to (and takes "bad <name>" for). Blank → just the name. |
| `BOT_FLAVOR` | no | `dale` (default) or `plain`: plain drops Dale's own catchphrases, GIF reflexes and /start blurb. |
| `BOT_PERSONA` | no | The persona prompt it starts with, until `/master_prompt` overrides it. Blank → Dale's. |
| `MANAGES_BOTS` | no | `true` (default) for the deployment that runs the others. The supervisor starts every other bot with `false`: they answer to their own names only, not the generic word "bot". |
| `DB_POOL_MAX` | no | Largest Postgres pool this process may hold (default 10; the other bots get 4). |
| `HUB_DATABASE_URL` | no | Where the bots hear each other. Unset → this bot's own `DATABASE_URL`, which is right for Dale. |

## Other bots

The owner can run more bots beside Dale, each its own Telegram account
with its own memory. Make the account in @BotFather (and turn its Group
Privacy off), then DM Dale:

    /newbot 123456789:AAH... Hank, hank hill
    sells propane, can't stand Luke's crypto talk

The first line is the token, its name, and any other names it answers
to; the lines after are a short description. Dale writes its persona
from that and from what the bots remember about whoever and whatever it
mentions, deletes the message (it holds the token), and registers the
bot. The `bots` service in
`docker/docker-compose.yml` (`python -m ipedro.supervisor`) then creates
its database (`ipedro_bot_<id>` on the same Postgres) and runs it.
`/bots` shows what's running; `/bot_stop`, `/bot_start` and
`/bot_remove` do what they say. A removed bot's database is kept.

The supervisor needs the Postgres user to be allowed to create
databases; the compose default user is.

**Postgres connections.** Every bot holds its own pool plus a few for the hub
(Dale's pool is up to `DB_POOL_MAX`=10, each other bot's is 4), against one
server whose `max_connections` defaults to 100. That is comfortable for a
handful of bots and runs out somewhere past a dozen; raise `max_connections`
(`command: postgres -c max_connections=200` on the `postgres` service in the
compose file) before adding many more.

**Bot admins.** A bot started by the supervisor inherits Dale's environment
apart from the few things set per bot (its token, database, name, persona,
`MANAGES_BOTS`, and not `EVOLVE_GITHUB_TOKEN`). That includes `ADMIN_USER_IDS`,
so Dale's bot admins are bot admins on every other bot too. The owner (the
one id who can `/newbot` and `/evolve`) works only on Dale.

**Who answers to "bot".** With several bots in a group, "bot, settle this"
could only mean all of them. So only Dale (the deployment with
`MANAGES_BOTS=true`) answers to the generic word "bot"; the others answer to
their own names, replies to them, and @mentions. A lone bot you run yourself
with the default `MANAGES_BOTS` still answers to it.

The full list of tunables (memory budgets, duckhunt parameters, etc.) is in
`.env.example`. Anything missing falls back to the defaults declared in
`ipedro/config.py`. Provider and model selections made at runtime via
`/ai_provider` and `/ai_model` are persisted in the `kv_store` table and
override the env defaults on the next startup.

## Bare metal

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m ipedro
```

A `systemd` unit is straightforward:

```ini
[Unit]
Description=iPedro V2
After=network.target

[Service]
WorkingDirectory=/opt/ipedro
EnvironmentFile=/opt/ipedro/.env
ExecStart=/opt/ipedro/.venv/bin/python -m ipedro
Restart=on-failure
User=ipedro

[Install]
WantedBy=multi-user.target
```

## Migrations

The schema in `ipedro/db/schema.sql` is applied idempotently at every
startup. There's no separate `alembic upgrade` step. The applied version
is recorded in the `schema_version` table.

## Importing legacy data

```bash
python -m scripts.migrate_legacy \
    --chat-ids ../iPedro/iPedro/data/chat_ids \
    --duckpoints ../iPedro/iPedro/data/duckpoint \
    --chat-history ../iPedro/iPedro/data/chat_history \
    --default-chat-id -1001273502662
```

Only paths that actually exist are imported. Run it **once**: only the chat-id
import is repeatable. The duck-point import adds the file's counts again on
every run, and the history import appends a second copy.

## Operational tips

- Watch `command_log` for unusual error frequency:
  ```sql
  SELECT command, COUNT(*) FROM command_log
   WHERE success = FALSE AND created_at > NOW() - interval '1 day'
   GROUP BY command ORDER BY 2 DESC;
  ```
- Vacuum the `messages` table periodically if you have very chatty groups.
- Rotate your `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, and
  `ANTHROPIC_API_KEY` immediately if they ever appear in a git history
  or log.
