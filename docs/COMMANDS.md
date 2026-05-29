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

### `/aiedit`, `/aivar`
Preserved from V1 for compatibility. Currently respond with a "not wired"
notice — the modern OpenAI SDK requires a different flow for image edits
and variations; these will be re-enabled when needed.

### `/aitranslate`
Reply to a voice note with this command to translate the audio to English.

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
bot's admin user. Fields:

| Field | Values |
|---|---|
| `policy` | `commands` \| `mention` \| `reply` \| `ambient` \| `always` |
| `ambient` | `0.0`–`1.0` probability used by the `ambient` policy |
| `persona` | `pedro` \| `neutral` \| any free-form name + custom prompt |
| `duckhunt` | `on` / `off` |
| `voice` | `on` / `off` — transcribe inbound voice notes |
| `memory` | `on` / `off` — store messages and build context from history |
| `ether` | `on` / `off` — opt this chat into cross-chat pager garbling (📟). Every ~hour, a recent message from any other ether-opted chat may be picked, garbled (dropped chars, leet subs, blackouts, truncation), and broadcast here with a spooky wrapper. Receiver cooldown: 4h. Needs ≥ 2 chats opted in. |

### `/ether <text>` (and on voice notes)

Manually transmit into the ether, as a **far-away radio voice** rather
than the ambient loop's garbled text:

- `/ether <text>` — your text is spoken aloud (OpenAI TTS) then put
  through a long-haul HF/SSB treatment so it sounds like a signal that's
  bounced 2000 miles off the ionosphere: a narrow comms passband,
  wandering pitch (slow drift + auroral warble), light grit/overdrive,
  smooth deep QSB fading (so the voice slips under the static and
  surfaces again), a drifting heterodyne tuning whistle, and a faint
  numbers-station bleed (a bundled "Swedish Rhapsody" recording) that
  drifts in and out as one interference layer. The voice stays on top and
  intelligible; every layer rides its own slow cycle so it never sounds
  like looping, switched effects. Broadcast as a voice note.
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

### Duckhunt

- `/duckhunt` — force-spawn a duck (requires duckhunt enabled for this chat).
- `/duckstats` — leaderboard for this chat.
- `/duckfriends` — your roster of ducks you've befriended in this chat.
- `/quackflag` — is there an active duck right now?

Reply tokens (case-insensitive): `bang`, `bef`, `ignore`.

#### How `bef` works

The AI plays the duck and decides whether it actually wants to be
friends. It's chaotic — usually agrees, occasionally refuses for absurd
or trivial reasons.

(Rarity tiers used to bias this decision toward refusal for rarer
ducks, plus an extra pre-AI dice gate. That whole layer is currently
disabled — every duck behaves identically. The column is preserved so
the tiering can be re-enabled later by flipping the helpers in
`ipedro/duckhunt/scoring.py` back to their lookup forms.)

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

Ducks may also wander off on their own at any time — more likely as they
hang around longer, and almost certainly gone after a day.

### "bad bot" / "bad pedro"
Reply to one of the bot's messages with either phrase to ask the bot to
delete that message.

## Admin (private DM, user 315660812)

These commands are silently ignored when used in groups, and reply with
"admin-only" when called by non-admin users in DM.

### `/list_chat_ids`
List chats the bot has been added to.

### `/send_message <chat_id> <text>`
Send a literal message to a known chat as the bot.

### `/logs`
Tail the in-DB command audit log.

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
Examples: `/ai_model claude-opus-4-7`, `/ai_model openai gpt-4.1-mini`.
Persisted in `kv_store`.

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
- `bypass_cooldowns` — skip the 15-second per-user cooldown on
  `bang`/`bef`/`ignore`.

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
