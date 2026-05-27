# Generic deployment notes

For Unraid-specific instructions see [`UNRAID.md`](UNRAID.md).

## Required environment

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | From `@BotFather` |
| `OPENAI_API_KEY` | yes | Modern OpenAI Python SDK |
| `DATABASE_URL` | yes | `postgresql://user:pass@host:port/db` |
| `ADMIN_USER_IDS` | no | `315660812` is always implicitly included |
| `OPENAI_*_MODEL` | no | Override default model names |

The full list of tunables (memory budgets, duckhunt parameters, etc.) is in
`.env.example`. Anything missing falls back to the defaults declared in
`ipedro/config.py`.

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

Only paths that actually exist are imported; the migrator is idempotent.

## Operational tips

- Watch `command_log` for unusual error frequency:
  ```sql
  SELECT command, COUNT(*) FROM command_log
   WHERE success = FALSE AND created_at > NOW() - interval '1 day'
   GROUP BY command ORDER BY 2 DESC;
  ```
- Vacuum the `messages` table periodically if you have very chatty groups.
- Rotate your `TELEGRAM_BOT_TOKEN` and `OPENAI_API_KEY` immediately if they
  ever appear in a git history or log.
