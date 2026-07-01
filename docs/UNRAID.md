# Unraid deployment

## Prerequisites

- Unraid 6.12+ with the Community Applications and Compose Manager plugins
  installed (the Compose Manager plugin is the easiest way to drive
  docker-compose from the Unraid UI).
- A Telegram bot token (`/newbot` from `@BotFather`).
- An OpenAI API key (used for embeddings, image gen, audio).
- An Anthropic API key (optional but recommended — used for text
  completions via Claude). Omit it and the bot falls back to OpenAI for
  text too.

## Layout

Pick a folder on a cache share, e.g. `/mnt/user/appdata/ipedro/`. Put the
contents of this repo there (or clone it).

```
/mnt/user/appdata/ipedro/
├── docker/
│   ├── docker-compose.yml
│   └── Dockerfile
├── ipedro/...
├── requirements.txt
└── .env          <-- you create this from .env.example
```

`POSTGRES_*` and `DATABASE_URL` in `.env` should refer to the compose service
name `postgres`. The example `.env.example` already does the right thing.

Postgres data is stored in a **host bind-mount** at
`/mnt/user/appdata/ipedro/pgdata` (override with `PGDATA_HOST_PATH` in
`.env`). This is on purpose: the Unraid **Appdata Backup** plugin only backs
up host paths under `/mnt/user/appdata/`. A *named Docker volume* (the old
default) lives in `/var/lib/docker/volumes/` and the plugin can't see it —
it logs `'docker_ipedro_pgdata' does NOT exist!` and skips it, silently
leaving your database out of every backup.

## Compose

```bash
cd /mnt/user/appdata/ipedro
cp .env.example .env
$EDITOR .env                       # set tokens, etc.
cd docker
docker compose up -d --build
```

The Postgres database lives in a host bind-mount under appdata
(`/mnt/user/appdata/ipedro/pgdata`) so the data survives container/image
rebuilds **and** gets caught by the Appdata Backup plugin.

### Migrating from the old named volume

Earlier versions used a named volume (`ipedro_pgdata`, which Docker Compose
exposes as `docker_ipedro_pgdata`). If you're upgrading, move that data to
the host path **before** starting the stack with the new compose, or
Postgres will initialize an empty database at the new path:

```bash
cd /mnt/user/appdata/ipedro/docker
../scripts/migrate_pgdata_to_appdata.sh        # copies; old volume left intact
docker compose up -d
```

The script copies (never moves) read-only from the old volume, so you can
roll back. Once you've confirmed the bot's data is present, reclaim the old
volume with `docker volume rm docker_ipedro_pgdata`.

Then point the **Appdata Backup** plugin at `/mnt/user/appdata/ipedro/`
(it'll now include `pgdata/`).

## Logs

```bash
docker compose logs -f bot
docker compose logs -f postgres
```

In the Unraid UI you can also click the container's log button.

## Updating

```bash
git pull
cd docker
docker compose build --pull
docker compose up -d
```

The schema is applied at startup; new schema versions are idempotent.

## Backup / restore

```bash
# Backup
docker compose exec postgres pg_dump -U ipedro ipedro \
    | gzip > /mnt/user/backups/ipedro-$(date +%F).sql.gz

# Restore
gunzip -c /mnt/user/backups/ipedro-YYYY-MM-DD.sql.gz \
    | docker compose exec -T postgres psql -U ipedro -d ipedro
```

## Reddit memes (`/redditmeme`)

Reddit blocks its anonymous `.json` API from server/datacenter IPs (HTTP
403), so `/redditmeme` uses Reddit's OAuth **application-only** (read-only)
access. You need a free Reddit "script" app:

1. Sign in to Reddit, go to <https://www.reddit.com/prefs/apps>.
2. Click **create another app…** at the bottom.
3. Name it anything (e.g. `ipedro`), choose type **script**.
4. Set **redirect uri** to `http://localhost:8080` (unused for read-only,
   but the form requires one).
5. Create it. The **client id** is the string just under the app name
   (near "personal use script"); the **secret** is the `secret` field.

Add both to `.env`:

```bash
REDDIT_CLIENT_ID=your_client_id
REDDIT_CLIENT_SECRET=your_client_secret
# Optional — a descriptive UA cuts throttling. Defaults to /u/moistifarius.
REDDIT_USER_AGENT=python:ipedro:1.0 (by /u/yourname)
```

Restart the stack, then run `/debug_redditmeme` (admin) in a chat: it
reports the mode (oauth vs anonymous), whether the token was obtained, and
the HTTP status of a real Reddit call — so you can tell at a glance if the
credentials are wired up. The bot caches the bearer token and reuses it
(re-minting only near expiry), and honors Reddit's rate limits.

## Troubleshooting

- **`extension "vector" does not exist`** — `pgvector/pgvector:pg16` ships
  the extension. If you swap the image for vanilla `postgres`, install
  pgvector or accept that semantic memory will be disabled (the bot logs
  a warning and continues).
- **Bot never replies in group** — by default the group response policy
  is `mention`. Mention the bot, reply to it, or set
  `/chat_config policy always` from the chat.
- **Duck never spawns** — duckhunt is off by default. Run
  `/chat_config duckhunt on` in the chat where you want it.
