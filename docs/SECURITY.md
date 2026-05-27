# Security notes

## Admin model

- The only built-in admin is Telegram user id **315660812**.
- Additional admins can be added via `ADMIN_USER_IDS=…,…`; usernames are
  **never** accepted — authorization is always by numeric id.
- Sensitive admin commands (`/list_chat_ids`, `/send_message`, `/logs`,
  `/memory_facts`, `/memory_forget`) are gated to **private DMs** by
  `auth.is_admin`. They are silently ignored in groups so bystanders are
  not even told the commands exist.

## Secrets

- All secrets live in environment variables, loaded by `pydantic-settings`
  from `.env`. `.env` is in `.gitignore`.
- `ipedro/logging_setup.py` redacts plausible secret-looking values (long
  opaque tokens) from log records whenever the line mentions a key term.
- `agents.md`, kept for historical context, **previously contained real
  Telegram and OpenAI keys**. If you re-use this repository, treat those
  keys as compromised and rotate them. (Removing the file from the working
  tree does NOT remove it from git history.)

## Hardening already in place

- The Docker image runs as a non-root user (`ipedro`).
- Postgres is reached only via the compose network unless you explicitly
  publish its port.
- Each command argument is validated before use; bad arguments produce
  friendly errors rather than tracebacks.
- Outbound OpenAI requests use bounded retries (`tenacity`) and never
  surface raw exceptions to the user.

## Known limitations

- `/aiedit` and `/aivar` are intentionally stubbed: the legacy API they
  used was deprecated. They are kept for command-name compatibility; the
  user receives a clear message.
- Voice transcription is disabled if the chat sets `voice off`. Voice
  notes themselves are downloaded to memory only — they are not persisted
  to disk.
- The semantic index (`ivfflat`) needs `ANALYZE` for best performance after
  bulk imports; this is a Postgres operational concern, not a bot bug.
