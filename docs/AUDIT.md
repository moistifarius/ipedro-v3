# iPedro Bot — Full Audit

**Date:** 2026-07-02 · **Commit audited:** `55c009c` (+ fixes below) · **Scope:** the whole `ipedro/` package, `docker/`, `docs/`, `tests/`.

This is a lasting record written as the maintainer loses AI assistance. It is
honest about what was verified, what was fixed, and what still needs a human's
eyes. Nothing here is speculative unless labelled so.

---

## 1. Executive summary

**Overall health: B+ (solid hobby-grade production bot).** The codebase is
unusually disciplined for a hobby project: pure logic is split from I/O and
well unit-tested (508 tests green), SQL uses parameterized queries throughout,
the async boundary around blocking ffmpeg/DSP work is handled correctly, and
error handling degrades gracefully almost everywhere. For a ~10-user private
Telegram bot the risk surface is small and mostly self-inflicted-only (a
malicious *member* is the threat model, not the internet).

**One real security gap was found and fixed this session** (config-wizard
authorization — see §3.1). The remaining items are operational hardening and
honest coverage gaps, not active bugs.

### Methodology note (important, read this)

The intended audit was a 15-agent parallel deep-read (one agent per
subsystem + cross-cutting reviewers, each with an adversarial verifier). That
job **hit the session's usage limit and produced zero output.** Rather than
lose the audit, it was redone **directly and sequentially**, prioritizing the
highest-yield risk classes across every file via targeted static scans plus
close reads of the flagged hot spots. This means:

- **Verified thoroughly:** SQL-injection surface (every interpolated query),
  callback authorization (every `@callback_query`), the async/blocking
  boundary, dependency pinning, container ops, config/docs env consistency.
- **Spot-checked, not line-by-line:** the 4,078-line `admin.py`, duckhunt
  service concurrency, ether SQL selection, the OpenAI retry predicate. No
  defects were found in the parts read, but a full line-by-line read of every
  file was not completed. See §7 for the honest gap list.

---

## 2. What was FIXED this session (committed)

| # | Fix | Severity | Commit |
|---|-----|----------|--------|
| 1 | **Config-wizard authorization** — `/config` + the `cfg:` callback had no auth check; any group member could flip a chat's settings, and a crafted callback could target *other* chats. Gated via `_can_edit_config` (bot admins anywhere; chat admins for their own chat only). +5 tests. | **High** | this session |
| 2 | **Dependency pinning** — `requirements.txt` was mostly `>=`; a future rebuild could pull a breaking `anthropic`/`openai`/`pydantic` major and brick the bot with no maintainer. Pinned to the exact tested versions. | **High (ops)** | this session |
| 3 | **Container log rotation** — compose had no log caps; an unattended bot's json-file logs grow unbounded and fill the Unraid disk over months. Added `max-size:10m max-file:5` to both services. | **Medium (ops)** | this session |

---

## 3. Findings

### 3.1 Security — config wizard was unauthenticated  ✅ FIXED

`on_cfg` (`ipedro/handlers/utility.py`, the `cfg:` callback) mutated
`chat_config` (duckhunt, memory, ether, response policy, persona, ambient
probability) with **no check on who pressed the button**. The `/config`
wizard's inline keyboard is a normal group message, so any member could press
an admin's buttons. Worse, the callback encodes `target_chat_id` (for the
DM-scoped `/config_for` flow), so a hand-crafted `cfg:<other_chat>:<field>`
callback could edit a *different* chat's settings.

**Fix:** `_can_edit_config(rt, user_id, host_chat, target_chat_id)` — bot
admins pass anywhere/any target (they legitimately drive `/config_for` from
DM); everyone else must be a chat admin/creator **and** may only touch the
chat the wizard lives in. Applied to both `/config` and `on_cfg`. Tests in
`tests/test_config_auth.py`.

### 3.2 SQL injection — NONE (verified safe)

Every dynamically-built query was traced. All identifier interpolation comes
from **controlled sources**, never user input:

- `memory/store.py::correct_name` — `{table}`/`{col}` are hardcoded literals
  passed by the function itself (`_fix("summaries","summary",…)`); the
  user-controlled `wrong`/`right` are handled in Python and never touch SQL.
- `db/repositories.py::update_config` — `{sets}` built from an `allowed`
  allowlist; values parameterized.
- `persona_state.py`, `admin.py::_set_duckstat_field` / duckstat editor —
  `{field}` gated by `_DUCKSTAT_EDITABLE_FIELDS`; values parameterized and
  clamped to INTEGER range.

Verdict: **no injection path.** Good discipline.

### 3.3 Callback authorization — otherwise solid (verified)

Every admin callback (`qchat:`, `dmsg:`, `dlast:`, `silch:`, `mfacts:`,
`mstats:`, `dse:`, `dsr:`, `mgm:`, `aip:`, …) routes through `_gate_callback`,
which correctly checks `is_admin_user`. The only unguarded one was `cfg:`
(§3.1, now fixed).

### 3.4 Async/event-loop — blocking work is offloaded (verified safe)

The obvious worry — ffmpeg + numpy/scipy DSP for `/ether` — is handled
correctly: `radio_fx.apply_radio_effect` runs the whole sync pipeline
(`subprocess.run` decode → DSP → encode) inside `asyncio.to_thread`, so it does
**not** stall the event loop. The bare `subprocess.run` calls
(`_decode_to_pcm` etc.) only execute inside that worker thread. No blocking
I/O was found on the main loop.

### 3.5 Cost profile — acceptable for 10 users, documented

Per **replied-to** message in a memory-enabled chat: 1 embedding (record the
user turn) + the main `chat()` reply + 1 embedding (record the bot turn), plus
a summary+fact-extraction pass every `SUMMARY_TRIGGER_MESSAGES` (default 80).
Meme asks add up to: 1 classifier (only if the fast grammar missed) + 1
query-distill + 1 judge, all on the cheap model. A generated meme = 1 cheap
text + 1 image gen. Nothing is unbounded-per-message, and reactions/cat-facts
short-circuit before the expensive path. **For ~10 users this is fine.** A
hostile member could spam `/a` or meme asks to run up cost — there is no
per-user rate limit (see §5). Low priority at this scale.

### 3.6 Unbounded in-memory dicts — low risk (noted)

`_PENDING_NAMING` (TTL-swept), `_RECENT_TRIVIA` (per-chat deque cap),
`_LAST_PICKED_CHAT`, `_PENDING_SEARCH_QUERIES`, `_PENDING_CUSTOM_VALUES`
(admin-only, tiny), `bot_messages._recent_sends` (per-chat deque). None grow
without bound in practice at this scale; the admin ones lack TTL sweeps but are
keyed by admin user id (a handful of entries, ever). Non-issue for this bot.

### 3.7 `except: pass` swallows — acceptable, reduces observability (noted)

~7 spots swallow exceptions silently, all around Telegram send/delete/react
calls where failure is genuinely non-fatal (message already deleted, user
blocked the bot, etc.). Reasonable, but they hide systemic problems. If
something "silently doesn't work," these are where to add a `log.debug` first.

---

## 4. Subsystem health (from the reads performed)

| Subsystem | Grade | Notes |
|-----------|-------|-------|
| Chat pipeline (`handlers/chat.py`) | A− | Intercept order is deliberate; meme/impersonation paths now record bot turns to memory symmetrically (fixed earlier this session). Reads cleanly. |
| Meme stack (`reddit.py`, `meme_finder.py`, `meme_sources.py`) | A− | Heavily tested (177 tests). OAuth token cache + back-off correct; multi-source hunt with vote ranking + AI judge; graceful degradation everywhere. The most-worked-on area of the session. |
| Memory + DB (`memory/*`, `db/*`) | A− | Parameterized SQL, schema back-fills are idempotent (`ADD COLUMN IF NOT EXISTS`), author-name JOIN correct. Embeddings can orphan after row edits but `_reembed` upserts corrected content. |
| Duckhunt (`duckhunt/*`) | B+ | Well-factored pure scoring; challenge lifecycle (1h abandon vs tight clock) is intentional. Concurrency races on stats are theoretically possible but harmless at 10 users. |
| Radio/ether (`radio_fx.py`, `ether.py`, `kiwisdr.py`) | B | Correct async offload; live SSB fetch is off by default. Complex DSP but well-commented. |
| AI client (`openai_client.py`) | B+ | Provider switching, cheap/main routing, cost logging, narrowed retry predicate (429s no longer multiplied — fixed earlier). Price table needs manual upkeep as models change. |
| Admin surface (`handlers/admin.py`) | B | 4,078 lines, all admin-gated. Spot-checked; not fully line-read (see §7). |
| Background loops (`bot.py`, `*_loops`, `on_this_day.py`, …) | B+ | Each loop catches its own exceptions and re-waits, so one bad tick doesn't kill the loop; per-chat iteration is isolated. Once-per-day stamps use the configured local timezone consistently. |
| Config/docs/ops (`config.py`, `docker/`, `docs/`) | B (was C) | Env vars consistent between config and docs. Deps now pinned, logs now rotated (this session). |

---

## 5. Operational recommendations (for the maintainer, prioritized)

1. **Backups (already fixed, verify it took):** Postgres is now a host
   bind-mount under `/mnt/user/appdata/ipedro/pgdata` so the Unraid Appdata
   Backup plugin catches it. **Confirm your next backup actually contains
   `pgdata/`** — this is the difference between "annoying" and
   "catastrophic." Also keep the `pg_dump` cron from `docs/UNRAID.md`.
2. **Pin is done — don't un-pin.** If you ever `pip install -U`, run
   `python -m pytest` before deploying. Anthropic/OpenAI SDKs break APIs
   between majors.
3. **Bot container has no healthcheck** (only `restart: unless-stopped`, which
   recovers *crashes* but not *hangs*). If you want auto-recovery from a
   deadlock, add a heartbeat: have the polling loop `Path("/tmp/ipedro.alive").touch()`
   each iteration and a compose healthcheck that fails if that file is >5 min
   stale. Not done here (needs a small bot-code change + test); documented as
   the next hardening step.
4. **No per-user rate limiting.** A hostile member could spam AI-backed
   commands to run up your OpenAI/Anthropic bill. At 10 trusted users this is
   theoretical; if it ever matters, add a per-(chat,user) cooldown in the
   `should_respond` gate.
5. **Set the optional keys** for the best meme results: `GIPHY_API_KEY`,
   `IMGUR_CLIENT_ID` (both free, see `docs/UNRAID.md`), and confirm
   `REDDIT_CLIENT_ID`/`SECRET` are set (Reddit blocks anonymous access from
   servers — `/debug_redditmeme` tells you the live status).

---

## 6. Verification evidence

- `python -m pytest` → **508 passed, 3 skipped** (skips are ffmpeg-not-on-PATH
  in this sandbox; ffmpeg IS installed in the Docker image).
- `python -m compileall ipedro/` → clean.
- `pip check` → no broken requirements.
- SQL scan: every `f"…{…}…"` adjacent to `db.execute/fetch` traced to a
  literal/allowlist source.
- Callback scan: every `@r.callback_query` cross-checked against its gate.
- No `TODO`/`FIXME`/`XXX`/`HACK` markers remain in `ipedro/`.

---

## 7. Honest gaps — what still deserves a human's eyes

These were **not** exhaustively line-read (the parallel deep-read was lost to
the session limit); nothing alarming was found in spot checks, but a careful
maintainer should eventually walk:

1. **`handlers/admin.py` end-to-end** (4,078 lines) — the duckstat editor
   confirmation flows (`dse:`/`dsr:`/`dsra:`) and picker pagination state.
   The confirmation tests (`tests/test_confirmation_flow.py`) cover the
   cancel-doesn't-mutate invariant, which is the scary one, and they pass.
2. **Duckhunt concurrency** — two users acting on the same duck in the same
   instant. Harmless at this scale (worst case: a double-count), but not
   transactionally guarded.
3. **Ether source/destination SQL** in `ether.py` — the eligibility/cooldown
   selection logic was read at a glance, not proven.
4. **The AI-generated meme quality** (`generate_meme`) — image models render
   caption text imperfectly; this is a quality caveat, not a bug. If results
   disappoint, an `imgflip` template API (needs a free account) would produce
   crisper image-macros than diffusion text.
5. **Model/price drift** (`openai_client.py` price table) — hardcoded; update
   it by hand when you change models or providers change pricing, or `/cost`
   will report stale numbers.

---

## 8. Bottom line

The bot is in good shape to run unattended. The one genuine security hole is
closed, the two operational time-bombs (unpinned deps, unbounded logs) are
defused, and backups were already moved onto a backed-up path earlier. The
test suite is a real asset — **if you change anything, run
`python -m pytest` before you deploy**, and trust it.
