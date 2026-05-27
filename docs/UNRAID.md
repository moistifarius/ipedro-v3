# Unraid deployment

## Prerequisites

- Unraid 6.12+ with the Community Applications and Compose Manager plugins
  installed (the Compose Manager plugin is the easiest way to drive
  docker-compose from the Unraid UI).
- A Telegram bot token (`/newbot` from `@BotFather`).
- An OpenAI API key.

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

## Compose

```bash
cd /mnt/user/appdata/ipedro
cp .env.example .env
$EDITOR .env                       # set tokens, etc.
cd docker
docker compose up -d --build
```

The Postgres database lives in a named volume (`ipedro_pgdata`) so the data
survives container/image rebuilds.

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
