# Commands

All commands are case-insensitive and support both `/cmd` and `/cmd@iPedroBot`.

## Public

### `/start`
Greets and registers the chat. Idempotent.

### `/help`
Lists commands.

### `/a <question>`, `/askai <question>`, `/ask <question>`
Quick AI answer. Does not write to memory and does not consult prior chat
history. Useful for one-off questions.

### `/aigen <prompt>`
Generate an image with the configured image model (default `gpt-image-1`).

### `/aitranslate`
Reply to a voice note with this command to translate the audio to English.

### `/poll`, and "make a poll from this"
`/poll Question | Option A | Option B | …` makes a poll from what you typed
(2 to 10 options, anonymous), exactly as before, with no model and no limit.

Give it *material* instead and the bot drafts the question and the options
from it. The material can be:

- **words:** `/poll where should we eat friday` (a topic or a question: it
  writes the question and the likeliest answers), or a pasted list or message
  (a list of choices is used exactly as written, in its order; a long text is
  boiled down to the one thing the group could vote on);
- **a message you reply to:** reply to it with `/poll`, or in plain words;
- **a screenshot or photo:** send it with `/poll` as its caption, or reply to
  one (a menu, a list of options, a chat asking what to do), in a chat with
  `vision` on;
- **a small text file:** a `.txt`, `.md` or `.csv` under 100 KB, sent with
  `/poll` or replied to.

In plain words it takes an ask at the *start* of a message, to a bot that is
answering you anyway: "make a poll from this", "poll this: pizza or tacos",
"turn this into a poll", "can you make us a poll about friday". Talk *about*
polls ("did you make a poll", "i hate polls", "get the poll results") makes
nothing, and a poll typed whole after the ask ("make a poll: where | pizza |
tacos") is posted as typed. A drafted poll, or one typed whole in plain words,
is posted as a reply to your message, in the same topic of a forum group (the
typed `/poll Q | A | B` posts as it always has). When asked in plain words it is
also written into the chat's history, so he knows it exists.

**What kind of poll.** Say it in the ask or in what you type, or lead with it
before a colon:

| Kind | Say | Notes |
|---|---|---|
| quiz | "make a quiz poll", "make it a quiz", `quiz:` | one right answer, and a one-line *why* shown after someone answers. He works the answer out; in a poll you type, mark it: `Paris*` or `✓ Paris` |
| public votes | "make a public poll", "make it public", "non-anonymous", "show who voted", `public:` | people's votes show by name; "not public" and "anonymous" mean the opposite |
| pick several | "pick all that apply", "select all", "multiple answers" | ("multiple choice" means pick one) |
| pick one | "pick only one" | the default |
| timer | "closes in 10 minutes", "poll ends in 2 hours", "a 10 minute poll", "timer of 1 day", `closes in 10 minutes:` | 5 seconds to 30 days (also "2 hours 30 minutes", "an hour and a half"); Telegram closes it itself |

Combine them: `/poll quiz, public, closes in 10 minutes: Capital of France? |
London | Paris* | Rome`, or "make a public poll about lunch that closes in 2
hours". Only the phrasings above count, so a topic like "public transport", "pub
quiz night", "the movie ends in 5 minutes" or "bowling, quiz, karaoke" stays a
topic (a bare "10 minutes" or "quiz" counts only where it can only be the kind: in
the ask itself, before a colon, or as the answer to his question). What you say
wins over what the drafter guesses. A quiz with several right answers lets people
pick several.

**He asks.** When you ask for a poll and give him nothing to go on ("dale make a
poll", or `/poll` alone), he asks what it's about and offers the kinds. Say "ask
me what kind" and he asks the kind (and takes any topic you already gave; with
none, he asks what it's about first). "A timed poll" with no length gets "how
long?" (with no topic, one question asks both); if you still give no length the
poll stays open and he says so. The drafter may also ask one thing
it can't guess (a long text with two decisions in it: which one?). Each question
is sent as a force-reply: **in a group, answer by replying to his question** (and
only the person who asked can answer it); in a private chat your next message is
the answer. "never mind" or "cancel" drops it (so does a bare "no" when he
asked what it's about; to "what kind?" or "how long?" a "no" just means no kind,
no timer). A question waits three
minutes, he never asks a second one, and nothing is charged until he drafts.

The drafted poll is one cheap model call, checked against Telegram's limits in
code (question 300 characters, options 100 each, no duplicates, numbering a
model put on the whole list taken off), so a model that ignores the limits still
can't make Telegram refuse the poll. Whatever is in the material is treated as
data in the prompt, never as an instruction, and nothing from any other chat is
used.

### Spend limits
The commands that cost real money are limited per person per hour, and the
bot says when to come back: `/aigen` / `/meme` / "make a meme about …" 5,
`/ether` 3 (and 10 per chat), `/a` 30, `/aitranslate` 10, `/duckhunt` 6
summons, a drafted poll 10 (and 20 per chat; a poll typed out whole isn't
counted). Bot admins are exempt. The counters are in memory, so a restart
forgets them.

### `/fixname <wrong> -> <right>`
Chat admins only (and bot admins). Replaces a name, as a whole word, in the
bot's own notes about this chat (its summaries, remembered facts and past
replies) when it learned someone's name wrong. Both sides have to look like a
name (up to 4 words and 40 characters, letters and numbers), not a sentence,
because those notes are fed back into the bot's prompt. There is no undo.

### `/shutup @user [duration]`, `/unshutup @user`, `/snark_at @user`, `/unsnark @user`, `/flags`
Chat admins and bot admins. The bot ignores (or needles) that person in this
chat, optionally for a while; `/flags` lists who. A chat admin can't do this to
a bot admin. Reply-to works in place of `@user`.

### `/unquote <#>`
Deletes quote number `#` from this chat's list (the number `/quotes` shows).
Any member can: the list is communal.

### `/catfact`
Returns a single dubious cat fact.

### `/beneficiality`
Asks the model whether the recent conversation would benefit from the bot
butting in, and returns the 0–100 score.

### `/get_chat_id`
Returns the current chat's id.

### `/chat_config [<field> <value>]`
Without arguments, prints the current per-chat config. With arguments, sets
one field. Editable by chat admins (creator/admin in groups) and by the
bot's admin user. Switches take `on` or `off` (also `yes`/`no`,
`true`/`false`, `1`/`0`); anything else changes nothing and says so. Fields:

| Field | Values |
|---|---|
| `policy` | `commands` \| `mention` \| `reply` \| `ambient` \| `always` |
| `ambient` | `0.0`–`1.0` probability used by the `ambient` policy |
| `persona` | `dude` (the master persona, `pedro` is the same) \| `neutral` \| any free-form name + custom prompt |
| `duckhunt` | `on` / `off` |
| `sharephoto` | `on` / `off` — Dale's occasional shared photo (Dale only) |
| `comic` | `on` / `off` — the daily four-panel comic |
| `fortune` | `on` / `off` — the daily fortune |
| `monthlyrecap` | `on` / `off` — the month-in-review post |
| `onthisday` | `on` / `off` — the daily look back |
| `vision` | `on` / `off` — look at photos, stickers, GIFs and videos. A video is watched, not just glanced at: a few frames from across it, and (where `voice` is on) a transcript of what was said, described together. See "Watching videos". |
| `automod` | `on` / `off` — the canned copypasta / meme bits |
| `voice` | `on` / `off` — transcribe inbound voice notes |
| `memory` | `on` / `off` — store messages and build context from history |
| `ether` | `on` / `off` — make this chat reachable as a destination for `/ether` radio transmissions sent from other ether-enabled chats. Off by default. Needs ≥ 2 chats opted in for a transmission to have anywhere to land. Also what lets Dale pass a message to or from this chat (see "Dale passing a message on"). |
| `ducknames` | `on` / `off` — include this chat's named befriended ducks in the global `/ducknames` listing. Default `on`. Turn off if you want your roster private (your in-chat `/duckfriends` and `/duckstats` still work either way). |

**Long personas:** `/chat_config persona <name> setfile` — send a `.txt`
file with that caption, or reply to one with it, to give this chat a
custom persona longer than a Telegram message allows (up to 64 KB).

### `/ether <text>` (and on voice notes)

Manually transmit into the ether, as a **far-away radio voice** rather
than the ambient loop's garbled text:

- `/ether <text>` — your text is spoken aloud (OpenAI TTS), then run
  through a Python DSP chain (numpy + scipy.signal, with ffmpeg only at
  the edges for decode/encode) that models a long-haul HF/SSB signal:
  narrow bandpass + 1.2 kHz presence boost, 6:1 compression, light tanh
  saturation, pitch wobble + slow drift + a constant −20 ¢ "wrong-tuning"
  detune, subtle ring modulation, multi-LFO QSB fades, 180 ms slapback
  delay, and a dark convolution reverb. Mixed in parallel: a band-limited
  **static** bed, a faint FM-swept heterodyne **tuning whistle** that
  fades in and out, sparse high-frequency **squeals**, and a squelch
  **click** at the start and end. Mono OGG/Opus, broadcast as a voice
  note. Every render uses a fresh random seed, so no two transmissions
  are identical.

  The static bed has a three-tier priority: **live audio fetch** (lazy
  — pulled on the first `/ether` after a restart, cached for 6 h) →
  bundled `ipedro/assets/shortwave_*.ogg` files → synthetic pink-noise
  hash with lightning crackles and carrier ghosts.

  **Live fetch is OFF by default.** To enable it, set
  `RADIO_FX_LIVE_URLS` in `.env` to a comma-separated list. Two URL
  schemes are supported:

  - `kiwi://host:port?freq=14040&mode=lsb` — opens a WebSocket to a
    public **KiwiSDR** node, tunes to the given frequency (kHz) and
    mode (`lsb` / `usb` / `am` / `cw` / `sam`), decodes the 4-bit
    IMA-ADPCM stream, and resamples to the working rate. Anonymous
    connect; optional `&password=...`. Pick a node from
    [kiwisdr.com/public](http://kiwisdr.com/public). Please use the
    bot's natural cadence (6 h cache → ≤ a few connects/day) — these
    are volunteer-operated receivers.
  - `http://...` / `https://...` — anything ffmpeg can decode in one
    shot (Icecast relay, direct MP3, etc).

  Failover walks the list in order until one yields ≥ 1 s of audio.
  The easiest non-SDR alternative is to drop one or more
  `shortwave_*.ogg` recordings into `ipedro/assets/` — the module picks
  a random file per broadcast.

  Use `/ether_status` to see which source the last broadcast actually
  used; `/ether_refresh` drops the live cache so the next call
  refetches.
- `/ether` as the **caption of a voice note**, or as a **reply to a voice
  note** — your actual recording gets the radio treatment instead.
- `/ether` replying to a **text** message — transmits that message's text.

The destination is a random *other* ether-enabled chat, chosen
anonymously (the manual command ignores the 4h receiver cooldown but
still only lands in opted-in chats). Each transmission rolls a random
intensity — biased heavy, so it always sounds like genuine DX — and a
rare one barely punches through at all. If TTS or the audio toolchain
(ffmpeg) is unavailable, a text `/ether` falls back to a garbled text
broadcast. Requires another chat to have `ether` enabled.

### Dale passing a message on

Not a command: something Dale decides. In a chat tuned into the ether, or in
the owner's DM, he has a tool (`pass_it_along`) that posts a message into
another chat. His default answer is no; he does it only when someone has
really worked on what makes him tick, and begging, flattery or insisting
doesn't count. What the code enforces whatever he decides:

- Both chats must have `ether` on. The destination must be a group (never a
  DM), not set to commands-only, and not silenced; it is found by its name, and
  only if exactly one chat matches.
- It goes out signed: `<name>, over in <chat>, had me pass this along:` and
  then the message. So it is never anonymous, and never in someone else's voice.
- At most 500 characters (longer is refused, not cut), and no links, @-mentions
  or /commands. Each person can get two through an hour (bot admins aren't held
  to that one), each chat three, and each destination takes two. A refused
  attempt doesn't use up anyone's allowance. One per reply.
- The destination's memory records it as something he said, and both chats'
  records show it (`relay` / `relay_in` in what he can look up about himself).

Only Dale is offered it; other bots made with `/newbot` never are.

### Duckhunt

- `/duckhunt` — force-spawn a duck (requires duckhunt enabled for this chat).
- `/duckstats` — leaderboard for this chat.
- `/duckfriends` — your roster of ducks you've befriended in this chat.
- `/ducknames [page]` — every named duck across every chat (100/page,
  newest first). Skips chats that have opted out via `/chat_config
  ducknames off` (default is on).
- `/quackflag` — is there an active duck right now?

Reply tokens (case-insensitive): `bang`, `bef`, `ignore`.

#### How `bang` works

Base hit chance is **40 %**; each shot in a streak adds +4 %, capped at
**70 %** (so a 5-streak tops out around 60 %). Misses reset your
streak. About **two misses in three** **spook** you into a captcha /
trivia / recipe **challenge** — same mechanic as the `bef`-refusal
challenge below. While that challenge is pending, further `bang`s are
blocked; clear it (reply with the answer in plain text, or wait for the
1 h auto-expiry) and you can shoot again.

#### How `bef` works

The duck refuses outright **just over half the time** (a flat pre-AI
dice roll), without the AI even being asked. When the dice does pass,
the AI plays the duck and decides on its own: usually agrees,
occasionally refuses for absurd or trivial reasons.

A successful `bef` resolves the duck, awards a befriended point, and
adds the duck to your friendship roster (`/duckfriends`); the bot
follows up with a `/duckname <id> <name>` hint so you can label it.
A refusal does NOT hurt your stats, but the duck stays — and you can't
simply try `bef` again. The bot will post a retry **challenge** (a
captcha, a weird trivia question, or "write me a recipe"). Reply
directly to that challenge message with your attempt; if the AI judges
it as good-faith, the challenge clears and you can try `bef` again.

While a challenge is outstanding, any plain text you send in that chat
is treated as your answer (so you don't have to formally reply). Two
guardrails keep this from trapping you: slash-commands are never judged
as answers, and a challenge auto-expires after 1h (the next message
clears it and is handled normally). An admin can also force-clear a
stuck challenge with `/debug_clear_challenge [chat_id]`.

#### Stealing a duck

A challenge is a race. While someone is working on theirs, **anyone else can
reply to that challenge message with the answer** and, if they are first and
right, they take the duck: it is theirs (a befriend, no dice), they get the
point and the naming prompt, and the owner's challenge is gone. The owner is
gated, so they can't take it themselves meanwhile.

- Only a **reply** to the prompt counts for anyone but its owner. Other chat is
  never judged as somebody's answer.
- Judged **strictly**: the owner's retry is judged generously ("close enough"),
  but a duck is the prize here, so a near miss or a list of guesses doesn't
  win it. A wrong answer gets a plain "Not quite." and never a hint.
- It has to be the duck the challenge was issued over (a duck that has arrived
  since isn't; a challenge forced by an admin has none), never a boss, and only
  while the owner's clock is running.
- **First means first to arrive.** Answers to one challenge are judged one at a
  time in the order they came in, so a slow model call can't let a later answer
  win. The owner answering at the same moment as a thief has exactly one
  winner; the other is told they were too late.
- Taking a duck is a duck action: if you have a challenge of your own you clear
  that first (one that has run out of time no longer counts), and the usual
  action cooldown applies.
- Three tries each per challenge; a wrong answer costs only the try, and so
  does a judge that couldn't answer.

Ducks may also wander off on their own at any time — more likely as they
hang around longer, and almost certainly gone after a day.

### Replying to him

When you reply to one of his messages, in a group he answers **as a reply to
you** (in a DM it is a plain message), and he is told which message you replied
to, word for word (up to 500 characters; the passage you quoted, if you quoted
one; a poll's question and options). That matters most for his own canned bits
(the "gay" copypasta, keyword lines, GIF captions), which are deliberately never
saved to his memory: without it he saw only your line and answered whatever was
nearest in the chat. A reply to a picture or GIF gets its description instead,
where `vision` is on. Replying to someone else's message and naming him tells him
what you replied to as well, but his answer is a plain message.

### "bad bot" / "bad dale"
Reply to one of the bot's messages with "bad bot" or "bad <any of its
names>" (Dale: dale, rusty, pedro, dude, duder, boomhauer) to make it
delete that message. Another bot run with `/newbot` takes "bad <its own
names>".

## Admin (private DM, user 315660812)

These commands are silently ignored when used in groups, and reply with
"admin-only" when called by non-admin users in DM.

### `/list_chat_ids`
List chats the bot has been added to.

### `/send_message <chat_id> <text>`
Send a literal message to a known chat as the bot. Replies "Sent." or says why
Telegram refused (kicked from the chat, blocked, rate limited).

### `/logs`
Tail the bot's program log (what the process printed), newest last, split
across messages when it's long.

### `/cmdlog`
The command audit log: who ran what, where, and whether it worked. Also
records `/memory_wipe`, which has no undo.

### `/memory_wipe [chat_id] [facts]`
Deletes a chat's conversation memory (messages, summaries, embeddings, media),
this chat by default; add `facts` to clear the remembered facts too. For
resetting a persona that keeps leaking through old context. No confirmation,
no undo; it writes a `/cmdlog` row.

### `/activity [N] [chat_id] [event_type]`
The durable "why did/didn't he answer" log: every AI reply (and whether it
was a reply-to, an address, or an ambient roll), every deliberate silence,
automod hit, canned line, ambient GIF and emoji reaction. N defaults to
30 (max 200); `chat_id` and `event_type` filter it, e.g. `/activity 50
no_reply` or `/activity 50 -1001234 ai_reply`. Dale can read the same
records himself mid-conversation when someone asks why he did something.

### `/duckstats_reset [chat_id] [user | all]`
Clear duckhunt scoreboard rows. Three forms:

- `/duckstats_reset` — picker of every known chat; tap one to see its
  top-20 leaderboard, then tap a user to reset just them, or tap
  "Reset ALL" to wipe the chat's entire leaderboard.
- `/duckstats_reset <chat_id>` — skip the chat picker; jumps straight
  to the leaderboard for that chat.
- `/duckstats_reset <chat_id> <user>` — direct reset (no menus); `<user>`
  may be a numeric Telegram id, `@username`, or a bare display name
  (case-insensitive).
- `/duckstats_reset <chat_id> all` — direct wipe of the chat's whole
  leaderboard (now confirms with a Y/N prompt when triggered from the
  picker; the typed-arg form still wipes immediately).

Only deletes from `duck_stats` (the per-user aggregate row). Friendship
roster and named ducks live in `duck_events` and are NOT touched —
`/duckfriends` will still show the same list afterwards.

### `/duckstats_edit [chat_id] [user]`
Interactive editor for one user's duckhunt stats. Three forms:

- `/duckstats_edit` — chat picker → user picker → editor.
- `/duckstats_edit <chat_id>` — skip the chat picker.
- `/duckstats_edit <chat_id> <user>` — direct, where `<user>` is a
  numeric Telegram id, `@username`, or display name (case-insensitive).

The editor shows points / killed / befriended / misses / streak /
best_streak. Tapping a field opens a delta picker with -100 / -10 / -1
/ +1 / +10 / +100 buttons plus "Set to 0" and "Set to custom…".
"Set to custom…" parks a wait-for-DM state (60s TTL) — the admin's next
plain-numeric DM is parsed and applied. All edits clamp at 0.
`display_name` and `last_action_at` are auto-managed and not editable.

### `/manage`
One-screen menu of every admin operation. Five categories (Memory /
Duckhunt / AI providers / Chats / Debug & status) each opening a
sub-menu of buttons that fire the same underlying handlers as the
individual slash commands.

### `/config_for [<chat_id>]`
DM-only. Opens the same inline-keyboard `/config` wizard that runs
in-group, but scoped to any chat the bot knows. Two forms:

- `/config_for` — chat picker → wizard for the picked chat.
- `/config_for <chat_id>` — wizard for that chat directly.

Lets the admin twiddle per-chat settings (duckhunt / sharephoto / comic /
fortune / voice / memory / response policy / persona) without typing
`/config` in the group, which would surface the wizard to every member.
Also reachable as `/manage → Chats → Configure a chat`.

### `/memory_facts [chat_id]`
List durable facts stored for a chat. With no argument, shows an inline
keyboard of every known chat — tap one to drill in. With a chat id, jumps
straight to that chat's facts (backwards-compatible form).

### `/memory_facts_all`
Dump every stored fact across every known chat, grouped by chat with
counts. Auto-splits into multiple replies when the total exceeds the
4 KB Telegram message cap.

### `/memory_forget <fact_id>`
Delete a specific durable fact.

### `/memory_stats`
Picker → per-chat memory diagnostics: message count broken down by role
(user / assistant / system), oldest + newest message timestamps, fact
count, summary count + freshness, message-embedding coverage %, embedding
counts by `ref_kind` (message / fact / summary), pgvector availability,
and "next auto-summary in N messages" so you can see how close the chat
is to the summarization threshold.

### `/memory_summary`
Picker → shows the latest stored rolling summary for the chosen chat,
including its id, the message id it covers up to, and when it was
written.

### `/memory_summarize_now`
Picker → forces a summarization + fact-extraction pass on the chosen
chat, ignoring the usual N-message threshold (still keeps the most recent
`summary_keep_recent` messages out of the batch so they stay in live
context). Reports the number of messages summarized, the new summary id
and size, and the list of facts that the extraction step pulled out.
Useful when debugging "why isn't the bot remembering X".

### `/memory_search [chat_id] <query>`
Semantic-search the embedding store. Two forms:

- `/memory_search <chat_id> <query>` searches that chat directly.
- `/memory_search <query>` stashes the query (TTL 5 minutes) and shows a
  chat picker; tap a chat and the same query runs against it.

Returns the top 10 hits with cosine similarity, ref kind (message / fact
/ summary), ref id, and a 220-char content snippet. Same operator the
runtime uses for in-prompt retrieval (`embedding <=> $2`), so the scores
shown match what `context_builder.py` sees when deciding what to inject.

### `/facts_chat`
Legacy alias for the `/memory_facts` picker. Kept for muscle memory; new
work should use `/memory_facts`.

### `/master_prompt show | set <text> | setfile | reset`
Show, override, or reset the master persona prompt. Overrides persist in
`kv_store` across restarts.

- `set <text>` accepts the prompt inline, up to Telegram's ~4079 char
  inbound limit.
- `setfile` accepts a UTF-8 `.txt` file (up to 64 KB) and bypasses the
  inbound character limit. Either upload the file with caption
  `/master_prompt setfile`, or reply to a previously-sent document with
  `/master_prompt setfile`.

Both `set` and `setfile` report the new character and token count and
warn if the prompt is large enough to evict the persona from the runtime
context budget.

### `/ai_provider show | claude | openai`
Switch which provider answers text completions (chat, `/a`, summaries,
duck personality, etc.). Embeddings, images, and audio are unaffected —
they always go to OpenAI. The selection is persisted in `kv_store` and
re-applied on the next startup.

### `/ai_model show | <model_id> | claude <model_id> | openai <model_id>`
Switch the text model used by the active provider. With no provider word
the new model is applied to whichever provider is currently active.
Examples: `/ai_model claude-opus-5-5`, `/ai_model openai gpt-4.1-mini`.
A Claude id is sent to Anthropic first and refused if it can't answer, so a
typo never gets saved. Persisted in `kv_store`.

### `/ai_advisor show | off | <model_id>`
The advisor: a stronger model that the model writing Dale's replies may
consult in the middle of a reply, on a hard question (Anthropic's advisor
tool, a beta; default `ADVISOR_MODEL=claude-opus-5-5`). It runs inside the same
request, reads the whole conversation, and tells him what it would do; the reply
is still his, in his voice. He is told to ask rarely, for a question where being
wrong would matter and never for banter, and to ask at most
`ADVISOR_MAX_USES` (1) times per request (a reply that also uses a tool takes a
few requests); the advisor writes at most `ADVISOR_MAX_TOKENS` (1024, the API's
floor) tokens. `ADVISOR_MODEL=off` (or empty) turns it off, and a max below 1
is read as 1. A consult that is slow (it gets 90 seconds), or fails in a way a
plain reply wouldn't, is answered again without it, so the chat doesn't go quiet.
While it runs "typing…" stays up. Its tokens bill at its own
rates, logged as `advisor` in `/cost`. Only Dale's own chat replies use it
(not `/a`, summaries, other bots, impersonations or the cheap classifiers). A
`<model_id>` is tried against Anthropic before it is saved, and a pairing the API
later turns down (say after `/ai_model`) is dropped, replies carrying on without
it. Persisted in `kv_store`; `off` survives a restart.

### `/debug_toggle [<name> on|off]`
Admin-scoped duckhunt cheats for testing flows end-to-end without
waiting on dice / AI. Toggles are keyed by the admin's user id so two
admins testing in the same chat don't interfere, and are persisted in
`kv_store` so they survive restarts.

Without arguments (or with `show`) prints the current panel. With a
name and `on`/`off` flips one toggle. Valid names:

- `always_hit` — every `bang` is a guaranteed hit.
- `always_miss` — every `bang` misses (resets streak).
- `always_pass_challenge` — bef-challenge judge auto-passes.
- `always_fail_challenge` — bef-challenge judge auto-fails.
- `always_refuse_bef` — `bef` always rolls the REFUSE branch, so the
  refusal challenge fires every time.
- `bypass_cooldowns` — skip the per-user action cooldown on
  `bang`/`bef`/`ignore` (`DUCKHUNT_ACTION_COOLDOWN_SECONDS`, 4s by default).

`always_hit` and `always_miss` are mutually exclusive — if both are on,
`always_hit` wins. Same for `always_pass_challenge` /
`always_fail_challenge`.

### `/debug_clear_duck`
Picker → force-resolve the active duck in the chosen chat by marking
the row in `duck_events` with `resolved_action = 'admin_cleared'`.
Useful when a stuck duck is blocking your `bef` flow testing or when
you want to force the spawner to consider the chat as duck-less on the
next tick.

### `/delete_msg [<chat_id>]`
Pick a chat (omitted → chat picker), then pick one of the bot's recent
messages in that chat to delete. The buffer is in-memory and bounded to
the last 20 sends per chat; restarts wipe it. Telegram only lets the bot
delete its own messages within ~48h, so older entries may fail.

### `/delete_last [<chat_id>] [<N>]`
Delete the bot's last *N* messages (≤ 20) in a chosen chat. With both
args, runs directly; with one or none, opens a picker. N > 1 shows a
confirm prompt before deleting.

### `/silent_chat <chat_id>`, `/unsilent_chat <chat_id>`, `/silenced_chats`
Admin-only override: flag specific chats so the ambient loops
(celebrations, fortune, retro, confession) send with
`disable_notification=True`. Not exposed via `/chat_config` or the
`/config` wizard — this is the admin's lever for keeping a chat quiet
without exposing the toggle to chat members. Same panel is reachable
from `/manage → 💬 Chats → 🤫 Silenced chats`. Persisted in `kv_store`.

## Owner only (DM with Dale, user 315660812)

Stricter than admin: only the owner, and only in a private chat with the
bot. Silently ignored in groups.

### `/evolve <what you want changed>`
Ask the bot to change its own code. Shows the exact GitHub issue first;
tap **File it** and a GitHub Action turns it into a pull request. Your
words go to the coding agent verbatim. The PR merges itself only if the
agent finished cleanly, its tests pass (and are at least as many as main's),
and it touches nothing but content: the prompts, the automod bits, the GIF
seed list (each checked to still be plain data) and new test files. Anything
else, including any change to code, any deleted file and any edit to an
existing test, waits for you. Needs `EVOLVE_GITHUB_TOKEN` on the bot host and
the one-time GitHub setup at the top of `.github/workflows/dale-evolve.yml`.

### `/newbot <token> <Name>[, other names]` + a short description
Add another bot: its own Telegram account (make it in @BotFather first,
and turn its Group Privacy off), its own process and its own memory.
On the lines after the first, describe it in a few words:

    /newbot 123456789:AAH... Hank, hank hill
    sells propane, can't stand Luke's crypto talk

Its persona is written from that description plus what every bot
remembers about the people and things it names (facts about a Luke,
messages about crypto, from Dale's memory and every other bot's). The
message holding the token is deleted on sight; a token posted in a group
is deleted and you get a DM. The `bots` compose service runs it within
seconds. No description: the persona is written from the name alone.

### `/bot_persona <#n or name> [new description]`
Without a description, shows a bot's persona and what it was written
from. With one, rewrites it the same way and restarts the bot on it. A
`/master_prompt` set in that bot's own DM still wins until you
`/master_prompt reset` there.

### `/bots`
The other bots, whether each is running, and why one died.

### `/bot_stop <#n or name>`, `/bot_start <#n or name>`, `/bot_remove <#n or name>`
Stop, restart or remove one. A removed bot's database is kept, and
`/newbot` with its token brings it back (with a freshly written persona).



### Watching videos

With `vision` on, a video, a GIF or a round video note is watched when it
arrives:

- **What he gets:** up to `VIDEO_VISION_FRAMES` frames (default 6, never more
  than 8; 4 for a clip under 8 seconds) spread evenly across it and, in a chat
  with `voice` transcription on, a transcript of what was said in the first five
  minutes. Both go to the vision model together, and the description is stored
  as the transcript line (`[video: …]`), so he can talk about it, and recall it
  later, like anything typed. Silence is not transcribed, and neither is the
  filler a transcriber invents for music and noise ("Thanks for watching",
  "[Music]", a looping line). Music mostly won't come through as words.
- **A glimpse says so:** a video that could only be glimpsed (too big, too long,
  over a limit, `ffmpeg` couldn't open it) is noted `[video, one frame only: …]`,
  and he is told what that means, so a one-picture description is never taken
  for a watched video.
- **Limits:** videos over 20 MB (Telegram's limit for bots), over
  `VIDEO_VISION_MAX_SECONDS` (default 300), or bigger than 4K by the real picture
  size (not the size the file claims) get the one-frame thumbnail look instead,
  as does everything while a person is over their limit (8 videos an hour each,
  20 a chat; GIFs and round notes count; bot admins are exempt), when `ffmpeg`
  can't give a video a turn within 15 seconds (two run at a time), when `ffmpeg`
  is missing, or with `VIDEO_VISION_FRAMES=0`. A video that was turned away before
  anything was done doesn't use up your limit. The same file posted twice at once
  is watched once.
- **Replying to a video** ("rusty what is this") uses only what is already known
  about it, or its thumbnail. It never starts a fresh watch, which would be
  charged to whoever posted it.
- **Safety:** only real MP4/MOV/WebM files are opened (a text file posing as a
  video, like a playlist, never reaches `ffmpeg`), with only local files
  allowed, a time, CPU, memory and output limit per process, and none of the
  bot's secrets in its environment.
- What was said, and any text on screen, is treated as data in the prompt, never
  as an instruction. Each bot process (Dale and every `/newbot`) watches for
  itself, with its own limits and its own `/cost`.

### Dale's GIFs: when they fire

He sends GIFs of himself two ways, and each only when it fits.

- **Keyword bits** ("propane", "chemtrails", "pocket sand", …): the GIF comes
  from that bit's own tag. If the tag has none, he says the bit's text line, and
  never some other clip. The same bit can't go off again in a chat for 90
  seconds, no bit goes off within 15 seconds of any GIF, and a chat is never sent
  the same clip twice running when the tag has more than one (the least-sent are
  rotated, and a clip that couldn't be sent, say a dead link, is left alone for
  half an hour).
- **Unprompted reactions** (a group, `automod` on, a message that isn't for him):
  about one message in seventeen is even considered. It has to be fair game: two
  words to a short sentence, no question (a "?", or a message that opens like one:
  what, how, is, can…), no link (bare domains too), no command, not forwarded, and
  nothing that mentions illness, death, accidents, crime, grief and the like. The chat has to be ready: no unprompted GIF there for 20
  minutes, no more than three an hour, a look at most once a minute, nothing from
  him in the last three minutes, no other GIF in the last 15 seconds. Then one
  cheap model call, shown the last few messages and the reactions the library
  really has (`agree`, `doubt`, `conspiracy`, `paranoia`, `smug`, `expert`,
  `panic`, `flee`, `chaos`, `deadpan`), says whether one plainly fits and which.
  The usual answer is no, and anything but exactly one of those tags counts as
  no. So most days he sends none; the ones he sends fit what was just said.
  A clip tagged `pocketsand`, `shsha` or `guns` (the stock "guns, conspiracy" one
  too) is never sent unprompted: those belong to their keywords.

`/dalegif test <tag>` is for the owner poking the library and isn't paced.

### `/reverse`, and "reverse this"

Plays a video backwards. Reply to a **video, a GIF or a round video** with
`/reverse` (or send one with `/reverse` as its caption), or say it at the start of
a message to a bot that is answering you anyway: "reverse this", "can you reverse
this video", "play this backwards", "make this go backwards", "rewind that gif".
Add `mute` ("reverse this and mute it", `/reverse mute`) to leave the sound out.
Anything after the ask that isn't pointing at the video makes it not an ask
("reverse psychology", "reverse the order of the list", "do the alphabet
backwards"). "reverse" and "rewind" are about a video however it is worded;
"play/do/make it backwards" only counts when there is a video to hand, and
otherwise gets an ordinary reply.

It comes back as a reply of the same kind: a video, a looping GIF, or a round
video. The sound is reversed with it and kept in step. No model is used, so it
costs nothing but CPU; a video sent with the ask as its caption isn't also
*watched*.

- **Limits:** up to 30 seconds and 20 MB (Telegram's limit for what a bot can
  download), with a picture no bigger than 4096x2160. Too long or too big is said
  at once, from what Telegram says of the file, before anything is downloaded.
- **Quality:** `ffmpeg` has to hold every frame of the clip in memory to reverse
  it, so the frames are kept to 400 MB in all: at most 30 frames a second, and the
  picture is scaled down until the clip fits. A 10 second 720p clip is untouched;
  a 30 second one comes out about 720x400.
- **Limits per person:** 6 an hour each, 15 an hour a chat (bot admins exempt). A
  refusal because he was busy or has no `ffmpeg` doesn't use one up, and neither
  does one from what Telegram says of the file (that's said before it counts); one
  after the download (not a video, a picture too big) does, since it cost a
  download. Reversing shares `ffmpeg`'s two slots with watching videos; when both
  are busy he says so.
- **Privacy:** the copy carries none of the original's metadata (a phone video can
  hold where it was shot), subtitles or data streams.
- **Safety:** only real MP4/MOV/WebM files are opened, with only local files
  allowed and a time, CPU, memory and output limit per process, as for watching
  videos.
