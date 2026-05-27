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

### Duckhunt

- `/duckhunt` — force-spawn a duck (requires duckhunt enabled for this chat).
- `/duckstats` — leaderboard for this chat.
- `/duckfriends` — your roster of ducks you've befriended in this chat.
- `/quackflag` — is there an active duck right now? (Rarity is hidden;
  discover it through interaction.)

Reply tokens (case-insensitive): `bang`, `bef`, `ignore`.

#### How `bef` works

`bef` is a two-stage decision:

1. A rarity-biased dice roll. If the roll fails, the duck refuses.
2. If the roll passes, the AI plays the duck and decides whether it
   actually wants to be friends. Rare and legendary ducks are snootier and
   more likely to refuse for absurd reasons.

A successful `bef` resolves the duck, awards a befriended point, and adds
the duck to your friendship roster (`/duckfriends`). A refusal does NOT
hurt your stats, but the duck stays — and you can't simply try `bef`
again. The bot will post a retry **challenge** (a captcha, a weird trivia
question, or "write me a recipe"). Reply directly to that challenge
message with your attempt; if the AI judges it as good-faith, the
challenge clears and you can try `bef` again.

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

### `/memory_facts <chat_id>`
List durable facts stored for that chat.

### `/memory_forget <fact_id>`
Delete a specific durable fact.
