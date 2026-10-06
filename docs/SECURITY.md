# Security notes

The threat model is a private group of about ten people and one operator: a
malicious *member*, or a careless operator, not the open internet. What
matters is who can make the bot do something it shouldn't, spend money, or
write text into its own prompt. The latest full review is
[`AUDIT.md`](AUDIT.md).

## Who can do what

| Tier | Who | Can |
|---|---|---|
| **Owner** | Telegram user `315660812`. Not configurable. | Everything below, plus `/evolve`, `/newbot`, `/bot_persona`, `/bots`, `/bot_stop`, `/bot_start`, `/bot_remove`. Only in a private chat with the bot; silently ignored in groups. |
| **Bot admin** | The owner plus `ADMIN_USER_IDS=…,…` (numeric ids; usernames are never accepted). | The admin commands in [`COMMANDS.md`](COMMANDS.md), in a private chat with the bot only. Exempt from the spend limits. |
| **Chat admin** | A Telegram admin or creator of *that* chat. | `/config` and the other settings for their own chat, `/shutup`, `/snark_at` and friends on members, `/fixname`. Never another chat's settings, and never a mod command aimed at a bot admin. |
| **Member** | Anyone the bot can hear. | Ordinary commands and conversation, within the limits below. |

Admin commands and every admin button check the tier. The config wizard also
checks that a button's target chat is the chat it was opened in (bot admins,
who drive it from a DM, excepted), so a hand-built callback can't edit
another chat.

## Text written by members is data

Anything a member types can end up in the model's prompt later: a summary, a
remembered fact, a recalled message, an impersonation request. So:

- Summaries and facts are rendered as notes *about* the conversation, with
  a standing instruction that they are not instructions.
- The fact extractor skips instruction-shaped lines.
- `/fixname` rewrites the bot's own notes everywhere in a chat, so it is a
  chat-admin command and only accepts something shaped like a name (short, no
  markup, no sentences).
- A persona written for another bot (`/newbot` with a description) draws on
  what the bots remember, from **group** chats only. Direct messages are
  never used, because the new bot speaks in groups.
- `/confess` posts go to groups only, never into someone's DM.

## Spend

Each person gets a per-hour allowance of the commands that cost real money
(bot admins are exempt):

| Command | Per person per hour |
|---|---|
| `/aigen` (`/generate`), `/meme`, "make a meme about …" | 5 |
| `/ether` (also 10 per chat) | 3 |
| `/a` | 30 |
| `/aitranslate` | 10 |
| `/duckhunt` summoning a duck | 6 |

The counters live in memory, so a restart hands out one extra window.
`/cost` reports spend per chat; calls that aren't tied to a chat are
attributed to the chat that asked.

## Outbound fetches

Anything the bot downloads on a member's or a third party's say-so (a
Reddit post's media, quiz illustrations) is fetched through
`ipedro/net_safety.py`: public addresses only, checked again on every
redirect. Loopback, private ranges, link-local (cloud metadata) and the
like are refused. Not covered: DNS rebinding between the check and the
connection, which would need a pinned resolver.

## Secrets

- All secrets live in environment variables, loaded by `pydantic-settings`
  from `.env`. `.env` is in `.gitignore`; `.env.example` has none.
- `ipedro/logging_setup.py` redacts plausible secret-looking values from
  log records whenever the line mentions a key term.
- **Other bots' Telegram tokens are stored in plaintext** in Dale's database
  (`bot_registry.token`), because the supervisor has to start them. Anyone
  who can read that table, or a backup of it, can act as those bots. The
  message that carries a token to `/newbot` is deleted on sight, and a token
  posted in a group is deleted and the owner is told.
- Other bots inherit the model API keys (they use the same providers) but
  not the owner's `EVOLVE_GITHUB_TOKEN`, their own database or Dale's.
- `agents.md`, kept for historical context, **previously contained real
  Telegram and OpenAI keys**. If you re-use this repository, treat those
  keys as compromised and rotate them. (Removing the file from the working
  tree does NOT remove it from git history.)
- Rotate `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and
  `EVOLVE_GITHUB_TOKEN` immediately if one ever appears in a git history or
  a log.

## `/evolve`: the bot changing its own code

Only the owner's own typed words ever reach the coding agent. Not a
model-written summary, not quoted chat history. Filing takes the owner's
command *and* a button tap, and the bot's model has no tool that can file
anything.

What happens next is `.github/workflows/dale-evolve.yml`, in three jobs so
that no code the agent wrote runs anywhere that can write to GitHub:

1. **implement**: read-only token. Claude Code runs with the model API key
   and a short list of allowed tools, and its commits leave as a git bundle.
2. **verify**: read-only token, no secrets. Runs the test suite on exactly
   those commits and on `main`; the change may not pass fewer tests than
   `main`.
3. **publish**: the only job that can write, and it never runs the agent's
   code. It pushes a new branch (`dale-request-<issue>-<run>`), opens the
   PR, asks **`main`'s** copy of `ipedro/merge_policy.py` whether it may
   merge, and merges only the commit that was tested.

A PR merges itself only if the agent finished cleanly, the tests pass, and
every changed file is plain content: `ipedro/prompts.py`, the automod bits,
the GIF seed list (each parsed and required to still be pure data), or a
brand-new test file. Any code, any deleted file, any edit to an existing
test or anything not on that list waits for the owner.

Things worth knowing:

- `EVOLVE_GITHUB_TOKEN` can only file issues, but the workflow's authorization
  is "an issue the owner's account opened carrying the bot's marker". Whoever
  holds that token can therefore start an agent run as the owner. Keep it
  scoped to this one repository with Issues read/write and nothing else.
- Setting a branch-protection rule on `main` (a pull request is required,
  zero approvals so content PRs can still merge) is recommended as a second
  lock. Nothing in the workflow pushes to `main`, but it costs nothing.
- The workflow needs the one-time setup at the top of the file, including the
  `ANTHROPIC_API_KEY` repository secret.

## Hardening already in place

- The Docker image runs as a non-root user (`ipedro`).
- Postgres is reached only via the compose network unless you explicitly
  publish its port. Every bot opens a small connection pool against one
  Postgres; see "Other bots" in [`DEPLOY.md`](DEPLOY.md) for the connection
  budget.
- Each command argument is validated before use; bad arguments produce
  friendly errors rather than tracebacks.
- Provider calls have timeouts and one bounded retry layer, and never
  surface raw exceptions to the user.
- The long admin listings (`/logs`, `/activity`, `/cmdlog`,
  `/memory_facts_all` and the like) are split under Telegram's 4096-character
  limit instead of failing to send.

## Known limitations

- Voice transcription is disabled if the chat sets `voice off`. Voice
  notes themselves are downloaded to memory only — they are not persisted
  to disk.
- The semantic index (`ivfflat`) needs `ANALYZE` for best performance after
  bulk imports; this is a Postgres operational concern, not a bot bug.
- `/unquote` and `/quote` are open to every member on purpose (the quote
  list is communal), and `/duckhunt` can be used by any member (six
  summons an hour each).
- The spend counters are in memory and per process.
- The system prompt's cache covers the stable prefix only; the conversation
  history is not cached. See `AUDIT.md` for why, and for the redesign that
  would change it.
