# Audit report

**2026-10-06** · audited commit `85db88d` (the merge of PR #17) · fixes on
`claude/laughing-ritchie-2UxbR` · scope: the whole system, code, deployment, docs
and the `/evolve` pipeline. The previous audit (2026-07-02) is kept at the end.

---

## 1. The short version

- **182 findings** from 15 reader agents, each checked by 1 to 3
  verifier agents trying to reproduce, refute and rate it. **145
  confirmed** (7 high, 61 medium,
  77 low), 25 split, 4
  refuted, 8 I checked by hand.
- After removing duplicates (the same defect reported from two angles) that is
  **150 real defects**. **142 are fixed** (3
  of them only in part); **8 are left alone** on purpose and listed in
  section 4 for you to decide.
- **Every high finding is fixed** (7 records, 5
  distinct defects): a model-name prefix match sent a parameter the current
  Claude models reject, so *every reply failed silently*; `/fixname` let any
  chat member rewrite the bot's own notes (and so write into its prompt); the
  automod "kys" bit answered a sincere "I want to kill myself" with "skill
  issue"; stored GIFs and videos could never be sent back; and the `/evolve`
  workflow could not run at all.
- Tests: **1315 → 1901**, green on Python 3.11 and on 3.12
  (the version the image ships). The new ones pin each fix, and several were
  checked against a real Postgres where the bug was a race or a SQL detail.
- **Nothing here is deployed, and nothing is merged to `main`.** The work is on
  `claude/laughing-ritchie-2UxbR`. Section 4 lists what needs you: a repository
  setting, a secret, and a handful of calls only you can make.

## 2. How it was done, and what it doesn't cover

**Readers.** Three slices, each with its own readers: *core* (the model client,
memory, who the bot answers, the hub), *trust* (authorization, secrets, the
`/evolve` pipeline, deployment, docs) and *features* (commands, duckhunt, ether,
reminders, quizzes, media). 15 readers produced 188 raw findings; 182 distinct
records remain once the same file and line were merged.

**Verifiers.** Each finding went to up to three verifiers with different jobs:
*reproduce* (trace the failing path, run a read-only snippet if possible),
*skeptic* (try to refute it: a guard elsewhere, a test that pins the opposite, a
comment saying it is intended) and *impact* (who can trigger it, how bad, will
the obvious fix break something). High findings got all three, medium two, low
one. A finding is **confirmed** when the first verifier confirms it and nobody
refutes it, **split** when they disagree, **refuted** when a verifier shows it is
not a defect.

**What went wrong, and what I did about it.** The session hit its usage limit
partway through the first pass and about two thirds of the verifier runs failed
(194 of 298 agent runs). Rather than trust unverified findings, I re-ran
verification as separate passes: medium and high first (108 verifier runs, all
completed), then low (84 runs, all completed). The low pass ran against a
frozen copy of the audited commit, so a fix couldn't make a real finding look
"refuted". Verifiers often rated a finding lower than its reader did; the
severity in the ledger is the verifier's.

**What it doesn't cover.**
- No second-opinion pass hunting for what the readers *missed*. Coverage is
  15 readers' worth.
- Nothing was load-tested, and nothing ran against live Telegram or a live
  Anthropic key. Where a bug was SQL or a race I checked it against a local
  Postgres 16 (leap-day birthdays, the quote numbering race, the activity-log
  prune). The `/evolve` workflow was linted (actionlint, shellcheck) and its
  bundle, diff and merge-policy pipeline was run end to end in throwaway git
  repos, but it has never run on GitHub; the first real `/evolve` is its test.
- The Claude Code CLI flags the workflow uses were checked against the CLI
  installed here (2.1.291, the version the workflow pins), not against a run.

## 3. What changed

Commits on `claude/laughing-ritchie-2UxbR` after `85db88d`
(`git log 85db88d..claude/laughing-ritchie-2UxbR`):

| Commit | Theme |
|---|---|
| `f160b3b` | The model parameter (the silent-mute bug), who may do what (`/fixname`, `/ai_model`), the "kys" bit, media file ids |
| `cf18058` | Answering the right people: follow-up openers, insult detection, the hub (basic groups, retries, policy), graceful shutdown |
| `17626f2` | Limits and sizes: per-person spend limits, `/ether` caps, one chunker for long replies, retries that never gave up |
| `df70185` | The `/evolve` pipeline and merge policy, the deployment files (`.env.example`, compose), the persona-eviction warning, outbound-fetch guard, pool and migration fixes |
| `7b96a35` | Ducks nobody saw, bots kicked from a chat, `/shutup` tiers |
| `524e166` | What the low-severity verification pass confirmed: truncated replies, switches, grammar, quote numbering, reminders, quizzes, token handling |
| `1f721e6` | Races, retries and things that only ever grew (boss hits, recap, activity-log retention) |
| the one that adds this file | The remaining documentation (MEMORY, UNRAID backups, COMMANDS, the migrator's claim), the canned-bit summarization turn, this report |

In plain terms:

- **The bot can't be muted by a parameter any more.** The thinking setting is
  chosen by exact model id (Sonnet 5.5 takes `between_tools`, Opus 5.5 can't
  turn it off), `/ai_model` asks Anthropic whether a model answers before it
  saves it, and a refusal is logged instead of becoming silence.
- **Member text is data.** Summaries, facts, recalled lines, impersonation
  samples, the persona notes and the judge/comic/recap prompts all say what
  they quote is not an instruction; `/fixname` and the persona writer can't
  be used to put a sentence into the prompt; persona generation never reads DMs.
- **Who may do what.** `/fixname` is a chat-admin command; a chat admin can no
  longer mute the owner; the generic word "bot" belongs to Dale alone, so two
  bots in one group don't both answer; `/help` and `/manage` only offer what a
  bot has.
- **Spend.** Per-person hourly limits on images, `/ether`, `/a`, translation and
  duck summons (bot admins exempt); spend is attributed to the chat that asked;
  one retry layer with real timeouts; the top-level cache write that was never
  read is gone.
- **`/evolve`.** The workflow now runs the Claude Code CLI directly (the action it
  used needed a GitHub App and an OIDC token it never had), in three jobs so that
  no code the agent wrote runs where a write token exists. The merge policy now
  parses the files it calls "content" and requires them to be data; tests match
  exact paths; deletions, symlinks and edits to existing tests wait for you; a
  run that failed or left uncommitted work never merges itself; the suite must
  pass at least as many tests as `main`.
- **Deployment.** `.env.example` finally exists; Compose reads `POSTGRES_*` from
  the right place (with a test that keeps the example, the compose file and the
  docs in step); the backup now covers every bot's database; CI runs the Python
  the image ships; dev dependencies are pinned.
- **Reliability.** Reminders, birthday greetings and recaps can't repeat because
  a bookkeeping step failed; races (quote numbers, boss hits, lost duck claims)
  are closed; things that retried forever (comic, hub, recap, `/evolve` filing)
  stop or back off; tables that only grew (`activity_log`) are pruned.

## 4. For you to decide or do

**Do (one-time):**
1. **Add the `ANTHROPIC_API_KEY` repository secret** and tick *Allow GitHub
   Actions to create and approve pull requests* (the steps are at the top of
   `.github/workflows/dale-evolve.yml`). Until then `/evolve` still files its
   issue, but the run fails at the agent step and says so on the issue. The
   first real request (a small content change is a good one) is the workflow's
   first real run: watch the three jobs go.
2. **Protect `main`** (require a pull request, zero approvals). Checked through the
   GitHub API on 2026-10-06: `main` has no protection and there are no rulesets,
   and the repository is public. The workflow no longer depends on it, since it
   only ever pushes its own `dale-request-*` branch, but it costs nothing, and
   "Automatically delete head branches" (also off) keeps those branches from
   piling up.
3. **If you run Compose already**, read the note in `docs/UNRAID.md` before the
   first `docker compose --env-file ../.env up`: any `POSTGRES_PASSWORD` or
   `PGDATA_HOST_PATH` you set in `.env` was being ignored, and now isn't.

**Decide** (left alone on purpose):
- `/unquote` is open to every member (communal by design, tested). Say if you
  want it chat-admin only.
- `/duckhunt` is public; summoning is now limited to 6 an hour each. A summon
  loop can't farm points, but anyone can still start a duck.
- The expired-challenge rule (any text is the attempt, for up to an hour) is the
  documented mechanic; the verifiers split on whether it is a defect.
- `/global_leaderboard` has no per-chat opt-out (the names do).
- **Bot tokens are stored in plaintext** in Dale's database (`bot_registry`), and
  so in every backup of it. The supervisor needs them. A removed bot's token is
  now blanked.
- **`EVOLVE_GITHUB_TOKEN` is worth more than "issues only"**: whoever holds it can
  start an agent run as you. Anything beyond plain content still waits for your
  review, but treat the token as write access to what the bot says.
- **Child bots inherit `ADMIN_USER_IDS`**, so your bot admins are admins on every
  bot. Documented; say if you want a per-bot list.
- **The conversation isn't cached** (only the stable prompt prefix is). Caching it
  means moving the clock, retrieved lines and style reminder to the end of the
  last user turn and putting a breakpoint before them. A real saving on a busy
  chat, and a change to every prompt, so I left it for you to choose.
- Smaller things left as they are: two active ducks in one chat in a
  millisecond race (a unique index would fail on existing rows); a hub-triggered
  reply builds a different cached prefix than a human-triggered one; the bef
  verdict is paid for even when the dice refuse; a quiz answer is committed
  before a failed message edit; the supervisor drops its output-pump reference
  after a 2 s grace; DNS rebinding between the outbound-URL check and the
  connection; Postgres `max_connections` (100) runs out somewhere past a dozen
  bots (documented in `docs/DEPLOY.md`).

## 5. The ledger

All 182 records, most severe first. *Verified* is how many verifiers
confirmed it out of how many ran. A **duplicate** is the same defect reported
from another angle; it is counted once in section 1. Line numbers refer to the
audited commit.

| # | Sev | Verified | Finding | What became of it |
|--:|:--|:--|:--|:--|
| 0 | High | confirmed 3/3 | /fixname is ungated: any chat member can rewrite every summary, fact and bot message in place, injecting arbitrary text into the stable system prefix (`utility.py:841`) | **Fixed**: Chat-admin only; the replacement must look like a name (short, no markup). |
| 1 | High | confirmed 3/3 | Prefix match sends thinking:disabled to claude-opus-5-5 / claude-sonnet-5-5; the 400 is swallowed into a silent no-reply for every chat (`openai_client.py:166`) | **Fixed**: Thinking parameter chosen by exact model id; /ai_model probes a model before saving it. |
| 2 | High | confirmed 3/3 | Automod 'kys' row answers a sincere first-person 'kill myself' with 'skill issue' / 'no u' (`automod.py:297`) | **Fixed**: The deflection is for the second-person insult only; a sincere first-person line gets no canned bit. |
| 3 | High | checked by hand | thinking:{type:disabled} is sent to claude-sonnet-5-5 / claude-opus-5-5 by prefix match; every reply 400s and the bot goes silent (`openai_client.py:157`) | **Duplicate**: Same as #1. |
| 4 | High | confirmed 3/3 | /fixname has no authorization: any member can whole-word-rewrite every summary, fact and bot message in the chat (`utility.py:841`) | **Duplicate**: Same as #0. |
| 5 | High | confirmed 3/3 | Media library stores the thumbnail's file_id for GIFs/videos/animated stickers, so sending them back always fails (`media_library.py:89`) | **Fixed**: The original file id is stored, so GIFs, videos and animated stickers can be sent back. |
| 6 | High | confirmed 3/3 | /evolve workflow cannot run the coding agent: `id-token: write` missing (and Claude GitHub App setup undocumented) (`dale-evolve.yml:37`) | **Fixed**: The workflow runs the Claude Code CLI directly (no OIDC or GitHub App), in three jobs. |
| 7 | High | confirmed 3/3 | /fixname lets any chat member irreversibly rewrite every summary, fact and bot message in the chat (and re-embed them) (`utility.py:840`) | **Duplicate**: Same as #0. |
| 8 | Medium | confirmed 2/2 | Model-extracted facts and the running summary enter the system prompt unframed, so a chat member's instruction-shaped message becomes a system-level directive (`context_builder.py:365`) | **Fixed**: Summaries and facts are rendered as notes about the chat, with a standing 'not instructions' line. |
| 9 | Medium | confirmed 2/2 | maybe_summarize has no per-chat lock; concurrent updates summarize the same batch twice and double-insert every extracted fact (`summarizer.py:49`) | **Fixed**: One summarization pass at a time per chat. |
| 10 | Medium | split 1/2 | Leak guard withholds any reply containing a tool's name while the capability brief hands the model those exact names, silencing the bot precisely on 'why did you ignore me?' (`openai_client.py:340`) | **Fixed**: The leak guard only withholds a tool call written out as text, not a reply that names a tool. |
| 11 | Medium | confirmed 2/2 | Follow-up openers 'nah', 'and', 'so', 'exactly', 'agreed', 'what' are a free YES for anyone in the 5-message window (`addressed.py:138`) | **Fixed**: A bare 'nah' / 'so' / 'why' counts as a follow-up only on the very next line. |
| 12 | Medium | split 1/2 | is_his_turn re-arms on every reply, turning one answer into an unbounded reply-to-every-message chain for that user (`addressed.py:247`) | **Fixed**: 'His turn' re-arms only for the very next line, so it can't chain. |
| 13 | Medium | confirmed 2/2 | Comic loop re-runs the Haiku + image-generation pipeline every 10 minutes when the Telegram send fails, with no stamp on permanent failure (`comic.py:80`) | **Fixed**: A permanent send failure stamps the day; transient ones back off. |
| 14 | Medium | confirmed 2/2 | Re-running /dalegif seed duplicates every seed GIF that has already been sent once (`dale_gifs.py:192`) | **Fixed**: Re-seeding keeps the URL of what was already sent. |
| 15 | Medium | split 1/2 | Paid commands any chat member can spam with no throttle: /aigen ($0.04/image), /ether (TTS + cross-chat voice note), /a (`ai.py:138`) | **Fixed**: Per-person hourly limits on the commands that cost money (bot admins exempt). |
| 16 | Medium | confirmed 2/2 | Ordinary-word automod rows ('based', 'linux', 'propane', 'objection', 'hello there', 'nl', 'sigma', '$420') shadow direct questions to the bot (`automod.py:362`) | **Fixed**: Canned bits fire only when a message isn't aimed at the bot (named, replied to, or in a DM). |
| 17 | Medium | split 1/2 | Bare 'bot' counts as this bot's name for every bot → two bots in one group both answer / both grudge (`chat.py:69`) | **Fixed**: Only the bot that runs the others answers to the generic word 'bot'. |
| 18 | Medium | split 1/2 | 'thanks man' between humans triggers Dale's canned send-off under every policy and puts the thanker on the hook (`chat.py:123`) | **Fixed**: 'thanks man' no longer counts as thanking the bot. |
| 19 | Medium | confirmed 2/2 | Plain-flavour bots say 'Sh-sha.' on meme-request misses and bef time-outs (`chat.py:430`) | **Fixed**: Plain bots' outgoing text has Dale's catchphrases taken out. |
| 20 | Medium | confirmed 2/2 | Ambient GIF roll eats 1-in-50 real answers in DMs and in 'always' chats (`chat.py:890`) | **Fixed**: No ambient GIF in DMs or in 'always' chats. |
| 21 | Medium | confirmed 2/2 | hub.listen: add_listener runs outside the try, so a failure there kills the hub task permanently and leaks the connection (`hub.py:128`) | **Fixed**: add_listener is inside the retry loop. |
| 22 | Medium | checked by hand | Other bots' hub lines are stored as human users (role='user', user_id=bot_id) and nothing downstream filters is_bot (`hub.py:241`) | **Fixed**: Monthly recap, on-this-day and impersonation leave bot-authored rows out. |
| 23 | Medium | confirmed 2/2 | In a basic (non-super) group, another bot's message_id is not this bot's message_id: memory rows get silently dropped/overwritten and replies quote the wrong message (`hub.py:245`) | **Fixed**: A bot's message id is stored only where ids are shared (supergroups). |
| 24 | Medium | confirmed 2/2 | hub.run gives up for the life of the process if its first connect or getMe fails (`hub.py:305`) | **Fixed**: hub.run retries with backoff. |
| 25 | Medium | confirmed 3/3 | thinking:disabled sent to current Sonnet/Opus ids by prefix → every reply silently vanishes, nothing in activity log (`openai_client.py:167`) | **Duplicate**: Same as #1. |
| 26 | Medium | confirmed 2/2 | Nested retry layers: SDK retries ×3 inside tenacity ×3, 600 s read timeout, no deadline on the reply path (`openai_client.py:378`) | **Fixed**: One retry layer (tenacity), SDK retries off, 45 s / 120 s timeouts. |
| 27 | Medium | confirmed 2/2 | Top-level automatic cache_control pays a write premium on the whole history every reply but is never read back (`openai_client.py:667`) | **Fixed**: Top-level cache_control removed (it was written and never read). Caching the conversation is a redesign: see section 4. |
| 28 | Medium | split 1/2 | Persona generation pulls facts and messages from every chat, including private DMs, and bakes them into a group bot's system prompt (`persona_gen.py:117`) | **Fixed**: A new bot's persona draws on group chats only, never DMs. |
| 29 | Medium | confirmed 2/2 | Insult detector flags ordinary speech ('my car is broken dude', 'the hinge is rusty and broken') → 24h snark toward innocent users (`user_flags.py:41`) | **Fixed**: The insult detector needs an insult aimed at the name; ordinary speech no longer earns a grudge. |
| 30 | Medium | confirmed 3/3 | Plain-flavor bots recite Dale's catchphrases in every duckhunt canned line (`duckhunt.py:130`) | **Fixed**: Same plainify filter as #19. |
| 31 | Medium | split 1/2 | /duckhunt is a public, unthrottled force-spawn: unlimited point farming and one AI call per spawn (`duckhunt.py:355`) | **Fixed**: Summoning a duck is limited to 6 an hour per person. (/duckhunt stays public.) |
| 32 | Medium | split 1/2 | Expired challenge keeps gating bang/bef/ignore for up to an hour and swallows the user's next normal message (`duckhunt.py:560`) | **Not changed**: Verifiers split: the 'any text is the attempt' rule is the documented mechanic. Left as is. |
| 33 | Medium | confirmed 1/1 | Quiz start/result photo sends are unguarded and a bad cached image bricks that quiz until an admin purges (`engine.py:190`) | **Fixed**: A refused image falls back to text, and a bad cached image is dropped. |
| 34 | Medium | split 1/2 | Anonymous confessions are surfaced into private DMs, not just groups (`ambient_loops.py:42`) | **Fixed**: Confessions surface in groups only. |
| 35 | Medium | confirmed 2/2 | Graceful shutdown always ends in RuntimeError('Polling is not started') and abandons in-flight handlers (`bot.py:248`) | **Fixed**: Own signal handlers, in-flight handlers drained, no more 'Polling is not started'. |
| 36 | Medium | confirmed 1/1 | TOKEN_RE rejects valid bot tokens whose last character is '-' (`bots.py:29`) | **Fixed**: Token regex uses lookarounds, so a token ending in '-' is accepted. |
| 37 | Medium | confirmed 2/2 | Comic loop never stamps a permanently undeliverable chat, so it regenerates an image every 10 minutes (`comic.py:80`) | **Duplicate**: Same as #13. |
| 38 | Medium | confirmed 2/2 | Comic loop regenerates a paid image every 10 minutes when the post fails permanently (`comic.py:95`) | **Duplicate**: Same as #13. |
| 39 | Medium | confirmed 2/2 | Ether destinations include chats the bot was removed from, so transmissions vanish after paying for TTS (`ether.py:160`) | **Fixed**: A chat that refuses us is taken off the ether and another is tried. |
| 40 | Medium | split 1/2 | /aigen, /aitranslate, /catfact and /beneficiality log OpenAI spend with chat_id NULL, so per-chat /cost under-reports (images are the priciest kind) (`ai.py:138`) | **Fixed**: Spend is attributed to the chat that asked. |
| 41 | Medium | confirmed 1/1 | /chat_config boolean fields treat any unrecognised value as OFF, so a typo silently disables memory/voice/etc. (`ai.py:287`) | **Fixed**: /chat_config on/off rejects a typo instead of reading it as off. |
| 42 | Medium | confirmed 2/2 | No size cap on /ether voice notes: a long recording drives multi-GB numpy allocations and minutes of CPU (`ether.py:438`) | **Fixed**: Voice notes over 60 s / 1.5 MB are refused. |
| 43 | Medium | split 1/2 | /ether has no rate limit or membership gate: unlimited TTS spend and anonymous cross-chat spam per member (and per DM stranger) (`ether.py:453`) | **Fixed**: /ether is limited per person and per chat, and capped in length. |
| 44 | Medium | confirmed 2/2 | Feb 29 birthdays/anniversaries cannot be set without a year (`utility.py:74`) | **Fixed**: Feb 29 can be set without a year and is observed on Feb 28 in common years. |
| 45 | Medium | confirmed 2/2 | Unbounded durations crash /remind, /tldr and /shutup with OverflowError (silent no-reply) (`utility.py:233`) | **Fixed**: Durations are bounded; no more OverflowError. |
| 46 | Medium | split 1/2 | /unquote has no authorization: any member can delete any quote in the chat (`utility.py:420`) | **Not changed**: Communal by design (documented and tested). Left open to every member. |
| 47 | Medium | confirmed 2/2 | /config wizard header interpolates the persona name unescaped under parse_mode=HTML; a chat admin can brick /config for the chat (`utility.py:1194`) | **Fixed**: The persona name is HTML-escaped in the wizard header. |
| 48 | Medium | confirmed 2/2 | Hub listener dies permanently if LISTEN fails right after connect (`hub.py:128`) | **Duplicate**: Same as #21. |
| 49 | Medium | confirmed 2/2 | Impersonation prefix match hijacks on pronouns/common words ('act like you…' → Youssef) (`impersonate.py:143`) | **Fixed**: The prefix match runs only for a single, non-ordinary word; 'act like you' no longer reaches Youssef. |
| 50 | Medium | confirmed 2/2 | Member-authored text is placed in the system prompt with the persona removed (prompt-injection path) (`impersonate.py:153`) | **Fixed**: Quoted samples are one line each, framed as quotations, and get no cache write. |
| 51 | Medium | split 1/2 | Other bots (hub posts) are impersonation targets: a plain bot can clone Dale's catchphrases (`impersonate.py:171`) | **Fixed**: Other bots' lines are not impersonation targets; plain bots' output is filtered. |
| 52 | Medium | confirmed 2/2 | Keyword fallback in media recall has no stop-words and no threshold, so the request's own verbs match and a wrong picture is sent (`media_library.py:142`) | **Fixed**: Keyword recall drops the request's own verbs and ranks by matches. |
| 53 | Medium | split 1/2 | Any chat member can trigger unmetered image generation; oversized topic also makes the paid result undeliverable (`meme_finder.py:187`) | **Fixed**: Image requests share the hourly limit and the topic is capped. |
| 54 | Medium | confirmed 2/2 | maybe_summarize has no per-chat lock: concurrent messages summarize the same batch and insert duplicate facts (`summarizer.py:55`) | **Duplicate**: Same as #9. |
| 55 | Medium | confirmed 2/2 | merge_policy treats a nested tests/test_*/conftest.py as plain content, so a conftest can auto-merge despite tests/conftest.py being guarded (`merge_policy.py:37`) | **Fixed**: Test paths match exactly; a conftest is guarded at any depth. |
| 56 | Medium | confirmed 2/2 | Monthly recap stats and quote pool count other bots' hub lines as people (`monthly_recap.py:96`) | **Fixed**: Same as #22. |
| 57 | Medium | confirmed 2/2 | /onthisday quotes the bot's own [photo: …] descriptions as a member's words, and length ordering makes them win (`on_this_day.py:102`) | **Fixed**: Bracketed rows (photo descriptions) and other bots are left out of the day's quotes. |
| 58 | Medium | confirmed 2/2 | 'Sh-sha.' hardcoded on /onthisday fallback and meme-miss replies that plain bots also run (`on_this_day.py:170`) | **Fixed**: Same plainify filter as #19. |
| 59 | Medium | split 1/2 | _withhold_leaked_internals silently drops any reply that merely mentions a tool name the prompt itself teaches the model (`openai_client.py:340`) | **Duplicate**: Same as #10. |
| 60 | Medium | confirmed 2/2 | Provider calls run with the SDKs' 10-minute timeout and double retry stacks; one stalled upstream pins handlers and the serialized hub listener for up to 90 minutes (`openai_client.py:378`) | **Duplicate**: Same as #26. |
| 61 | Medium | split 1/2 | persona_gen launders chat content from every group into another bot's system prompt (`persona_gen.py:132`) | **Duplicate**: Same as #28. |
| 62 | Medium | confirmed 2/2 | Reddit post URL is downloaded from any host/scheme with redirects followed (SSRF into the operator's LAN) (`reddit.py:188`) | **Fixed**: Downloads go through a public-address-only guard, redirect hops included. |
| 63 | Medium | confirmed 1/1 | Meme-request grammar fires on casual mentions after a comma and on hyphenated 'meme-' (`reddit.py:360`) | **Fixed**: A topic-less ask must end the message; 'meme-free' is not a meme. |
| 64 | Medium | confirmed 1/1 | Reminder is re-sent if marking it fired fails, and the rest of the batch is delayed (`reminders.py:309`) | **Fixed**: Delivery and bookkeeping are separate: tracking or marking can't resend or stall the batch. |
| 65 | Medium | confirmed 2/2 | Auto-grudge now fires on 'rusty'/'dale' used as ordinary words (regression from the identity refactor) (`user_flags.py:39`) | **Fixed**: Same detector rewrite as #29, including the vocative 'dude'. |
| 66 | Medium | split 1/2 | main has no branch protection or rulesets on the live repo, although the workflow names that rule as the only thing keeping un-PR'd code off main; the job runs with contents: write (`dale-evolve.yml:15`) | **Not changed**: Needs a repository setting only the owner can make. The workflow no longer depends on it (it pushes only its own branch). |
| 67 | Medium | checked by hand | The agent does hold a GitHub token: the action writes its token into the checkout's remote URL and env; the test pins only the YAML (`dale-evolve.yml:46`) | **Fixed**: The agent job has no GitHub token at all; the writer job never runs its code. |
| 68 | Medium | confirmed 2/2 | 'Collect the work' commits whatever is on disk, so abandoned or cut-off edits can be auto-merged (`dale-evolve.yml:109`) | **Fixed**: A swept (uncommitted) commit, or an agent that failed, never auto-merges. |
| 69 | Medium | confirmed 2/2 | A re-run of the same issue can merge the previous run's failing PR: push rejection is ignored and the judge step runs anyway (`dale-evolve.yml:141`) | **Fixed**: One branch per run; the tested commit is pinned through to the merge. |
| 70 | Medium | confirmed 2/2 | The automod no-ReDoS house rule is enforced nowhere, automod.py is CONTENT, and matching runs synchronously on the event loop against member text (`automod.py:22`) | **Fixed**: Structural no-ReDoS tests over every pattern, and matching runs on bounded text. |
| 71 | Medium | confirmed 2/2 | File tap can leave a request stuck in 'filing' forever: cb.answer() runs after the atomic claim, and any non-FilingError after the claim is unhandled (`evolve.py:149`) | **Fixed**: Stuck 'filing' requests are recovered; the tap handles any failure. |
| 72 | Medium | confirmed 2/2 | Verify keys only on pytest's exit status and every tests/test_*.py is CONTENT: a content-only PR can short-circuit the gate, crash startup, or run side effects on the operator's machine (`merge_policy.py:37`) | **Fixed**: The suite must pass at least as many tests as main; new tests only; existing ones guarded. |
| 73 | Medium | confirmed 2/2 | docker-compose variable interpolation never reads the repo-root `.env` the docs tell users to create (`docker-compose.yml:60`) | **Fixed**: .env.example added; the compose file and docs say to use --env-file ../.env; tests pin them. |
| 74 | Medium | split 1/2 | /confess text is reposted into a random chat drawn from ALL known chats, including private DMs and groups that never opted in (`ambient_loops.py:42`) | **Duplicate**: Same as #34. |
| 75 | Medium | confirmed 2/2 | Supergroup migration drops the old chat's config/state when the new id already has a row — and it usually does (`chat_migration.py:85`) | **Fixed**: On migration the old chat's settings win for one-row-per-chat tables. |
| 76 | Medium | confirmed 2/2 | Pool retry wrapper re-executes non-idempotent statements after a mid-flight connection loss (`pool.py:81`) | **Fixed**: A write is not replayed after a mid-flight connection loss. |
| 77 | Medium | confirmed 2/2 | /logs chunker emits a ›4096 chunk whenever one ring entry (a traceback) is longer than 3800 chars (`admin.py:836`) | **Fixed**: One chunker that never exceeds Telegram's limit. |
| 78 | Medium | split 1/2 | /cmdlog has no length guard while its /manage twin truncates; long stored errors push it past 4096 (`admin.py:862`) | **Fixed**: /cmdlog uses the same chunker. |
| 79 | Medium | confirmed 3/3 | /activity reply exceeds Telegram's 4096-char cap for any N above ~35; documented example fails (`admin.py:906`) | **Fixed**: /activity uses the same chunker. |
| 80 | Medium | confirmed 2/2 | /ai_model persists an unvalidated model id to kv, which then overrides .env on every restart (`admin.py:1823`) | **Fixed**: Same probe as #1. |
| 81 | Medium | confirmed 2/2 | /memory_facts_all sends an empty first message (and oversized chunks) when one chat's facts exceed 3900 chars (`admin.py:2173`) | **Fixed**: Same chunker as #77. |
| 82 | Medium | confirmed 2/2 | Any Telegram user can drive the bot's paid APIs from a DM; no per-user or per-chat limit on image generation, Sonnet, vision or meme hunts (`ai.py:131`) | **Duplicate**: Same as #15. |
| 83 | Medium | confirmed 2/2 | /ether lets any member push anonymous audio/text into another chat with no per-user limit and TTS cost per call (`ether.py:240`) | **Duplicate**: Same as #43. |
| 84 | Medium | confirmed 1/1 | /quote seq allocation is not race-safe and nothing enforces per-chat uniqueness (`utility.py:351`) | **Fixed**: Numbers are allocated under a per-chat advisory lock (checked on real Postgres: 40 concurrent saves, 40 distinct numbers). |
| 85 | Medium | confirmed 2/2 | Hub posts write another bot's message_id into this bot's messages table; in basic groups that id space is not shared, so rows collide and the dedupe silently corrupts embeddings (`hub.py:244`) | **Duplicate**: Same as #23. |
| 86 | Medium | confirmed 2/2 | hub.run gives up permanently if bot.me() or the hub DB connect fails once at startup (`hub.py:303`) | **Duplicate**: Same as #24. |
| 87 | Medium | confirmed 2/2 | Per-bot connection budget can exhaust Postgres max_connections once several bots run (`hub.py:305`) | **Fixed**: Pools are sized (10 for Dale, 4 for the others) and the connection budget is documented. |
| 88 | Medium | split 1/2 | Persona is silently dropped from the prompt when it exceeds CONTEXT_MAX_TOKENS; MEMORY.md says it is always kept and the 64 KB chat-persona path has no warning (`context_builder.py:379`) | **Fixed**: A dropped block is logged as a WARNING; MEMORY.md no longer claims the persona is always kept. |
| 89 | Medium | confirmed 2/2 | merge_policy CONTENT auto-merges executable Python modules, so a "content-only" /evolve PR can carry arbitrary startup code with no review (`merge_policy.py:378`) | **Fixed**: Content modules are parsed and must be data only; the logic moved to guarded files. |
| 90 | Medium | confirmed 2/2 | persona_gen pulls facts and embeddings from every chat — including private DMs — into a child bot's system prompt that then speaks in groups (`persona_gen.py:348`) | **Duplicate**: Same as #28. |
| 91 | Medium | split 1/2 | Unpinned dev dependencies run inside the privileged /evolve CI job that can auto-merge to main (`requirements-dev.txt:2`) | **Fixed**: Dev dependencies pinned; the CI job that runs them no longer holds a write token. |
| 92 | Low | confirmed 1/1 | Capability brief promises picture recall unconditionally, but filing and recall only run when memory is on (`capabilities.py:80`) | **Fixed**: A memory-off chat is told it keeps no pictures. |
| 93 | Low | confirmed 1/1 | force_summarize stores facts even when the summary call returned nothing, re-inserting the same facts on every admin retry (`summarizer.py:156`) | **Fixed**: A failed summary skips fact extraction, like the automatic pass. |
| 94 | Low | confirmed 1/1 | embedding_dim is stored, displayed and used to size vector(N) in the schema but never sent to the embeddings API, so any non-default model/dim pairing silently kills semantic memory (`openai_client.py:920`) | **Fixed**: Embeddings are requested at EMBEDDING_DIM and checked, with one clear error. |
| 95 | Low | confirmed 1/1 | A bot-to-bot reply opens the human follow-up window in every participating bot, so the next human line can draw a reply from each bot (`bot_messages.py:60`) | **Fixed**: A bot-to-bot reply doesn't open the human follow-up window. |
| 96 | Low | confirmed 1/1 | _NOT_INHERITED only strips the process environment; pydantic re-reads the stripped keys from a .env in the cwd (bare-metal/dev runs) (`bots.py:43`) | **Fixed**: A child's environment blanks the owner's secrets so a .env can't re-supply them. |
| 97 | Low | confirmed 1/1 | Child bots inherit ADMIN_USER_IDS (and every other setting) undocumented, so Dale's admins are silently admins on every other bot (`bots.py:162`) | **Fixed**: Documented: child bots inherit ADMIN_USER_IDS. |
| 98 | Low | confirmed 1/1 | /bot_remove leaves the removed bot's token in bot_registry indefinitely (`bots.py:270`) | **Fixed**: Removing a bot blanks its token. |
| 99 | Low | checked by hand | Unbounded meme topic can push the miss reply past Telegram's 4096-char limit (uncaught send error) (`chat.py:429`) | **Fixed**: Meme topics are capped. |
| 100 | Low | confirmed 1/1 | Automod intercept skips the summarization opportunity the other non-AI exits take (`chat.py:764`) | **Fixed**: The automod exit gives summarization its turn. |
| 101 | Low | confirmed 1/1 | Reply cap of 300 output tokens with no stop_reason check posts truncated replies while the prompt invites long ones (`chat.py:1089`) | **Fixed**: A reply cut off at the token cap is trimmed to its last sentence (cap now 400). |
| 102 | Low | confirmed 1/1 | /manage catalog advertises /newbot, /bots and /bot_* on bots whose router doesn't include them (`command_catalog.py:374`) | **Fixed**: /manage and /help list /evolve and /newbot only on the bot that has them. |
| 103 | Low | confirmed 1/1 | LISTEN connection has no command_timeout, so a half-open socket stalls the keepalive probe for the kernel's TCP timeout instead of 30s (`hub.py:147`) | **Fixed**: The LISTEN connection has a 15 s command timeout. |
| 104 | Low | confirmed 1/1 | Hub replies ignore the 'reply' response policy (`hub.py:252`) | **Fixed**: Hub replies go through the chat's own response policy. |
| 105 | Low | confirmed 1/1 | Hub-triggered replies build a different cached prefix than human-triggered replies in the same chat (no tools, different capability brief) (`hub.py:266`) | **Not changed**: Cost only: a hub-triggered reply builds a different cached prefix than a human-triggered one. Left. |
| 106 | Low | confirmed 1/1 | Impersonation turns put a per-member sample block in the 1h-TTL cached prefix, paying a 2x write for an entry that is almost never reused (`context_builder.py:339`) | **Fixed**: An impersonation turn asks for no cache write. |
| 107 | Low | confirmed 1/1 | Monthly recap re-spends a full main-model call every hour on a transient send failure and posts the fallback recap when the AI is down (`monthly_recap.py:345`) | **Fixed**: A model that is down early in the month waits; a failed send reuses the text already paid for. |
| 108 | Low | split 0/1 | /cost mis-prices opus-5-5 and the model picker omits the current ids, so the one config change that breaks replies is also the one /cost can't price (`openai_client.py:84`) | **Fixed**: opus-5-5 is priced and the model picker lists current ids. |
| 109 | Low | confirmed 1/1 | Leaked-internals filter guards only the tool loop; the plain chat path (impersonation, hub replies, OpenAI-less fallbacks) posts ‹thinking› leaks verbatim (`openai_client.py:626`) | **Fixed**: Stray ‹thinking› markup is withheld on the plain path too. |
| 110 | Low | refuted 0/2 | Child stop grace (15s, sequential) exceeds Docker's 10s stop grace and is shorter than aiogram's 30s long poll, so deploys SIGKILL every child (`supervisor.py:35`) | **Not a defect**: Verifiers: Compose's stop grace and the supervisor's drain are compatible. |
| 111 | Low | confirmed 1/1 | Docs and admin help state a 15-second action cooldown; code uses 4 seconds; always_hit/always_miss documented for ignore but only applied to bang (`config.py:164`) | **Fixed**: Docs say the cooldown is the setting (4 s); the toggle docs say bang only. |
| 112 | Low | confirmed 1/1 | Lost bang/bef race still tells the loser 'You shot the duck! +1' while awarding nothing (`service.py:276`) | **Fixed**: A shot that loses the race is told there is no duck. |
| 113 | Low | confirmed 1/1 | Boss duck: a hit landing after the kill replies 'Hit on the BOSS duck (4/3). +1' and pays points on a dead duck (`service.py:316`) | **Fixed**: A hit after the kill or after the boss left pays nothing and shows no '(4/3)'. |
| 114 | Low | confirmed 1/1 | Spawn path is check-then-write; two active ducks per chat are possible and the older one resurfaces as a ghost (`spawner.py:153`) | **Not changed**: Two active ducks in one chat in a millisecond race. A unique index would fail on existing data, so it is left. |
| 115 | Low | checked by hand | Duck row is inserted before the announcement; a failed send leaves an invisible active duck for hours (`spawner.py:155`) | **Fixed**: A duck whose announcement fails to send is retired. |
| 116 | Low | confirmed 1/1 | /ducknames with a huge page number crashes the handler (OFFSET exceeds int64) (`duckhunt.py:453`) | **Fixed**: /ducknames pages are capped. |
| 117 | Low | confirmed 1/1 | Duck names accept raw multi-line text and are broadcast to every chat with the owner's Telegram first name; /global_leaderboard has no opt-out at all (`duckhunt.py:531`) | **Partly fixed**: Duck names are one line. /global_leaderboard still has no opt-out: a decision for you. |
| 118 | Low | confirmed 1/1 | Every bef pays for an AI verdict whose output is discarded on the 55% dice-refusal path and on every REFUSE (`duckhunt.py:702`) | **Not changed**: Cost only: a verdict call is paid even when the dice refuse. Left. |
| 119 | Low | confirmed 1/1 | Challenge judge prompt lets the answer rewrite the verdict (chat text framed as instructions) (`prompts.py:78`) | **Fixed**: The judge prompt says the answer is data. |
| 120 | Low | confirmed 1/1 | Send-then-stamp loops re-post when the stamp write fails after a successful Telegram send (`celebrations.py:87`) | **Fixed**: A delivered greeting is not re-posted because the stamp failed. |
| 121 | Low | confirmed 2/2 | Feb 29 birthdays cannot be entered without a year and are only celebrated in leap years (`celebrations.py:167`) | **Duplicate**: Same as #44. |
| 122 | Low | confirmed 1/1 | Celebration loop retries a permanently undeliverable chat every 5 minutes all day (`celebrations.py:201`) | **Fixed**: A chat that refuses the greeting is stamped for the day. |
| 123 | Low | split 0/1 | Per-bot Postgres connection budget (13 each) can exceed the image's default max_connections=100 with a handful of bots (`pool.py:67`) | **Duplicate**: Same as #87. |
| 124 | Low | confirmed 1/1 | activity_log has no retention: one row per message in every group, forever (`repositories.py:447`) | **Fixed**: activity_log is pruned after ACTIVITY_RETENTION_DAYS (90). |
| 125 | Low | confirmed 1/1 | /chat_config has no 'vision' field and its help/status omit it, while the /config wizard exposes a vision toggle (`ai.py:221`) | **Fixed**: /chat_config covers vision, on-this-day and monthly recap. |
| 126 | Low | confirmed 1/1 | /chat_config ambient accepts 'nan' and stores 1.0 (100% ambient) (`ai.py:249`) | **Fixed**: /chat_config ambient rejects nan and inf. |
| 127 | Low | confirmed 1/1 | /debug_persona still compares against DEFAULT_DUDE_PROMPT, so every non-Dale bot reports a phantom /master_prompt override (`debug.py:286`) | **Fixed**: /debug_persona compares against the bot's own starting persona. |
| 128 | Low | confirmed 1/1 | /evolve File button: a non-FilingError from file_issue leaves the request stuck in 'filing' (`evolve.py:155`) | **Duplicate**: Same as #71. |
| 129 | Low | confirmed 1/1 | /shutup silently ignores an unparseable duration and mutes indefinitely (`mod.py:88`) | **Fixed**: An unreadable /shutup duration is refused, not read as forever. |
| 130 | Low | checked by hand | A Telegram chat admin can /shutup the bot owner or bot admins (trust-tier inversion, chat-scoped) (`mod.py:89`) | **Fixed**: Only a bot admin may /shutup or /snark_at a bot admin. |
| 131 | Low | confirmed 1/1 | /remind text is uncapped, so a near-max-length reminder is permanently dropped when it fires (`utility.py:240`) | **Fixed**: /remind text is capped at 1000 characters. |
| 132 | Low | confirmed 1/1 | Quote seq allocation is racy: no UNIQUE(chat_id, seq), so concurrent /quote can produce duplicate numbers and /unquote N then deletes both (`utility.py:354`) | **Duplicate**: Same as #84. |
| 133 | Low | confirmed 1/1 | /echo with reply-to mishandles the topic: one-word topics are ignored and multi-word topics lose their first word (`utility.py:776`) | **Fixed**: /echo as a reply keeps its whole topic. |
| 134 | Low | confirmed 1/1 | Wizard 'clear custom' leaves persona=‹custom name› with no text, silently reverting to the master prompt while the header still shows the custom name (`utility.py:1143`) | **Fixed**: 'clear custom' leaves a persona that exists. |
| 135 | Low | confirmed 1/1 | /anniversary note is capped at two words by split(None, 3): longer names make the date unparseable (`utility.py:1369`) | **Fixed**: /anniversary notes may be several words. |
| 136 | Low | confirmed 1/1 | Hub LISTEN connection has no command timeout, so a silently dropped connection stalls the keepalive for the TCP retransmit timeout (`hub.py:147`) | **Duplicate**: Same as #103. |
| 137 | Low | confirmed 1/1 | Hub replies ignore the chat's response_policy except 'commands' (`hub.py:253`) | **Duplicate**: Same as #104. |
| 138 | Low | confirmed 1/1 | Silent except: pass sites that hide a lost record or a stale admin panel (`hub.py:294`) | **Partly fixed**: The hub's silent excepts now log. Harmless 'message not modified' passes in admin panels remain. |
| 139 | Low | confirmed 2/2 | Hub join is one-shot: a transient failure at startup disables bot-to-bot hearing for the whole process (`hub.py:306`) | **Duplicate**: Same as #24. |
| 140 | Low | checked by hand | hub.run gives up for the life of the process on any transient startup failure (`hub.py:308`) | **Duplicate**: Same as #24. |
| 141 | Low | confirmed 1/1 | Recall caption uses the global master prompt instead of the chat's resolved persona (`media_library.py:183`) | **Fixed**: Recall captions use the chat's own persona (quiz verdicts too). |
| 142 | Low | confirmed 1/1 | Recap is sent before it is stamped; a stamp failure or restart re-posts it next hour (`monthly_recap.py:335`) | **Duplicate**: Same as #107. |
| 143 | Low | confirmed 1/1 | Comic and year-in-review prompts splice raw chat content into the prompt with no data framing (`prompts.py:128`) | **Fixed**: The comic, recap and TL;DR prompts say the chat text is data. |
| 144 | Low | confirmed 1/1 | Quiz flow: a failed message edit is swallowed after the answer was already committed, stranding the taker (`engine.py:114`) | **Not changed**: A quiz answer is committed before a failed message edit; the taker re-taps. Left. |
| 145 | Low | confirmed 2/2 | Quiz image warm-up is an untracked loop.create_task: failures are never logged and a purged cache can stay empty (`engine.py:145`) | **Fixed**: The quiz warm-up task is tracked and its failures logged. |
| 146 | Low | split 0/2 | Live shortwave fetch asks for 30 s of audio inside a 25-30 s budget, so realtime sources time out (`radio_fx.py:71`) | **Fixed**: The live fetch asks for 15 s inside a 25 s budget and keeps partial audio. |
| 147 | Low | confirmed 1/1 | KiwiSDR passwords in RADIO_FX_LIVE_URLS are logged and echoed in /ether_status (`radio_fx.py:169`) | **Fixed**: Passwords in live-source URLs are redacted from logs and /ether_status. |
| 148 | Low | confirmed 1/1 | Reddit video mux does synchronous temp-file writes/reads of up to 64 MB on the event loop (`reddit.py:903`) | **Fixed**: The Reddit video mux does its file I/O in a thread. |
| 149 | Low | confirmed 2/2 | /remind with a huge duration raises OverflowError and the user gets no reply (`reminders.py:259`) | **Duplicate**: Same as #45. |
| 150 | Low | confirmed 1/1 | Share-photo loop is hard-wired to 'the Dude' persona for every bot identity (`sharephoto.py:363`) | **Fixed**: The share-photo loop (the Dude's bit) runs for Dale only. |
| 151 | Low | refuted 0/2 | Supervisor's sequential 15 s per-child stop grace exceeds Docker's default 10 s stop timeout (`supervisor.py:35`) | **Not a defect**: Verifiers: not a defect (same as #110). |
| 152 | Low | confirmed 1/1 | Supervisor drops the output pump reference after a 2 s grace while the pump may still be reading (`supervisor.py:168`) | **Not changed**: Supervisor drops its output-pump reference after a 2 s grace. Cosmetic; left. |
| 153 | Low | refuted 0/1 | Every /newbot child process inherits EVOLVE_GITHUB_TOKEN and registers the /evolve router, so plain bots can file 'Dale request' issues and hold the PAT needlessly (`bot.py:158`) | **Not a defect**: Verifiers: a child does not inherit the token. (Only Dale registers /evolve now.) |
| 154 | Low | confirmed 1/1 | Plain bots only skip DaleGif rows; a plain-string Dale catchphrase row in CONTENT automod.py is spoken by every BOT_FLAVOR=plain bot, and the only pins are content-tier tests (`automod.py:549`) | **Fixed**: Plain-flavour output is filtered, and the bits are data-only. |
| 155 | Low | confirmed 1/1 | Admin help and catalog drift: /newbot family advertised on bots that don't register it, /evolve missing from the admin help, quiz aliases undocumented (`basics.py:141`) | **Fixed**: Same as #102. |
| 156 | Low | confirmed 1/1 | /help from a bot admin in a group posts the entire admin command index into the group (`basics.py:253`) | **Fixed**: The admin /help is posted in a private chat only. |
| 157 | Low | confirmed 1/1 | Owner-facing text says 'Nothing merges without you' while the workflow auto-merges content-only PRs (`evolve.py:175`) | **Fixed**: The owner is told that plain-content changes can merge themselves. |
| 158 | Low | confirmed 1/1 | The persona-writer's 'quoted as data, not instructions' framing lives in CONTENT prompts.py, not in the guarded module that depends on it (`prompts.py:212`) | **Fixed**: persona_gen frames its notes as data in guarded code. |
| 159 | Low | confirmed 1/1 | `.env.example` is referenced by every setup path but does not exist; compose hard-fails without a `.env` (`README.md:41`) | **Fixed**: .env.example exists and a test keeps it complete. |
| 160 | Low | confirmed 1/1 | AUDIT.md's verification numbers and 'docs consistent' claims are stale and now misleading (`AUDIT.md:178`) | **Fixed**: This report replaces the stale numbers. |
| 161 | Low | confirmed 1/1 | COMMANDS.md/README describe commands that do not exist or do the wrong thing (`/logs`, `/mood`, chat_config fields) (`COMMANDS.md:176`) | **Fixed**: COMMANDS.md and README corrected (/logs, /cmdlog, /mood, every /chat_config field). |
| 162 | Low | confirmed 1/1 | Documented model defaults and env table are wrong for the current config (claude-sonnet-4-6 vs claude-sonnet-5; new bot/hub/evolve vars missing) (`DEPLOY.md:15`) | **Fixed**: DEPLOY.md's model defaults and env table corrected. |
| 163 | Low | confirmed 1/1 | MEMORY.md describes a memory design the code no longer matches (fact cap, stores, threshold placement) (`MEMORY.md:20`) | **Fixed**: MEMORY.md rewritten against the code. |
| 164 | Low | confirmed 1/1 | SECURITY.md omits the owner tier, the /evolve PAT and that bot tokens live in plaintext in Dale's DB (and thus in the documented backups) (`SECURITY.md:5`) | **Fixed**: SECURITY.md rewritten. |
| 165 | Low | confirmed 1/1 | UNRAID.md backup/restore commands cover only Dale's database; every other bot's memory (`ipedro_bot_‹id›`) is left out (`UNRAID.md:98`) | **Fixed**: UNRAID.md backs up the whole server (pg_dumpall). |
| 166 | Low | confirmed 1/1 | EVOLVE_GITHUB_TOKEN is documented as "issues only", but the workflow's sole authorization is the issue sender, so that PAT is effectively write-and-auto-merge authority (`config.py:80`) | **Fixed**: Documented what the PAT is worth (SECURITY.md, config.py). |
| 167 | Low | confirmed 2/2 | /send_message only catches TelegramBadRequest; Forbidden/RetryAfter errors leave the admin with no feedback (`admin.py:802`) | **Fixed**: /send_message reports every way Telegram can refuse. |
| 168 | Low | confirmed 1/1 | Error toasts built from exception text can exceed Telegram's 200-char callback-answer limit and raise inside the except (`admin.py:1005`) | **Fixed**: Error toasts are cut to fit Telegram's limit. |
| 169 | Low | confirmed 2/2 | /config_for ‹unknown chat_id› raises an uncaught ForeignKeyViolationError; admin gets no reply (`admin.py:1142`) | **Fixed**: /config_for says it has never seen that chat. |
| 170 | Low | confirmed 1/1 | delete_last picker (N=1) always toasts 'Deleted.' even when nothing was deleted (`admin.py:1571`) | **Fixed**: The delete-last toast says what happened. |
| 171 | Low | confirmed 1/1 | /memory_search misparses any query whose first word is a number as '‹chat_id› ‹query›' (`admin.py:2519`) | **Fixed**: '/memory_search 2019 road trip' is a search, not a chat id. |
| 172 | Low | split 1/2 | /memory_wipe deletes a chat's entire memory with no confirmation and no command_log entry (`admin.py:2684`) | **Partly fixed**: /memory_wipe writes a /cmdlog row. No confirmation tap, on purpose (documented, admin-only). |
| 173 | Low | confirmed 1/1 | Display-name lookup in /duckstats_reset and /duckstats_edit only sees the first word of the name (`admin.py:2766`) | **Fixed**: Display names of several words resolve. |
| 174 | Low | confirmed 1/1 | Owner can only ever see the first 1,500 chars of a generated persona; the comment claims /bot_persona shows the rest (`bots.py:95`) | **Fixed**: /bot_persona shows the whole persona. |
| 175 | Low | confirmed 1/1 | /bot_start, /bot_stop, /bot_remove and /bot_persona echo their argument back, so a pasted token is repeated in a reply (`bots.py:187`) | **Fixed**: A token pasted into /bot_* is not echoed back. |
| 176 | Low | confirmed 1/1 | /evolve claim can be stranded in 'filing' with no recovery path (`evolve.py:145`) | **Duplicate**: Same as #71. |
| 177 | Low | checked by hand | hub.handle_post answers in chats the bot has left and ignores the chat's `reply`-only policy (`hub.py:250`) | **Fixed**: A bot kicked from a chat stops answering there (and hub replies follow the chat's policy). |
| 178 | Low | confirmed 1/1 | Chat-controlled text reaches the model in the system role (summary, facts, replied-to image description, impersonation samples) (`context_builder.py:359`) | **Fixed**: Same framing as #8, plus the impersonation fix (#50). |
| 179 | Low | confirmed 1/1 | silenced_chats mutates the in-memory set before the kv write, so a failed persist leaves the process and the DB disagreeing (`silenced_chats.py:62`) | **Fixed**: silenced_chats rolls back when the write fails. |
| 180 | Low | refuted 0/2 | Supervisor shutdown cannot finish inside Docker's 10 s stop grace: child bots are SIGKILLed on every stack stop/restart (`supervisor.py:35`) | **Not a defect**: Verifiers: not a defect (same as #110). |
| 181 | Low | confirmed 1/1 | scripts/migrate_legacy.py is documented as idempotent but re-running it double-counts duck stats and duplicates chat history (`migrate_legacy.py:66`) | **Fixed**: The migrator's docs no longer claim it is idempotent. |

---

# Appendix: the previous audit (2026-07-02)

> Kept for history. Its numbers (508 tests, "docs consistent") describe that
> day, not today; section 1 above is current.

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
