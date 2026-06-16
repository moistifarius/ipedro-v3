"""Single source of truth for every command surfaced through /manage.

The /manage hub is rendered ENTIRELY from this catalog: top-level
categories, per-category submenus, and the per-command "card" screens
that show a description + a copy-pasteable usage hint for free-text
commands. Adding a new command later means appending one ``Command``
row here and (if it's interactive) wiring its mgm: action callback in
admin.py — no menu code to touch.

Two flavors of leaf:

* ``action=None`` — a *card* leaf. Tapping it shows the description and
  the usage line in a <code> block so the admin can copy it. Used for
  free-text commands (``/remind <duration> <text>``, ``/aigen <prompt>``,
  etc.) since inline keyboards can't gather their arguments.
* ``action="mgm:..."`` — a *wired* leaf. Tapping it routes to an
  already-implemented mgm: callback (a chat picker, a status panel, a
  toggle screen, etc.). The button label still comes from this catalog
  so the help text and the menu stay in sync.

Categories appear in the /manage top menu in the order they are
*first* introduced below. Commands within each category render in the
order they appear here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    slug: str          # unique key; embedded in callback as ``mgm:cmd:<slug>``
    category: str      # category key (basics, ai, duckhunt, …)
    name: str          # leading-slash command name (``/remind``)
    usage: str         # one-line usage hint for the card (``/remind <duration> <text>``)
    desc: str          # one-line description (shown in card body)
    action: str | None = None  # when set: route to this existing mgm: leaf


@dataclass(frozen=True)
class Category:
    key: str           # short key (matches Command.category)
    label: str         # full button label including emoji
    blurb: str         # one-line caption shown above the submenu


# Order here is the order categories appear in the /manage top menu.
CATEGORIES: tuple[Category, ...] = (
    Category("basics",    "🛠 Basics",            "Core commands and settings."),
    Category("ai",        "🤖 AI",                "Chat, image, voice, snark."),
    Category("duckhunt",  "🦆 Duckhunt",          "Game commands + admin spawns."),
    Category("quotes",    "💬 Quotes & lore",     "Quotes, lexicon, chat stats."),
    Category("reminders", "⏰ Reminders & dates",  "Timers, birthdays, polls."),
    Category("dm",        "🤫 Anonymous (DM)",    "Confessions and private notes."),
    Category("mod",       "🛡 Moderation",         "Shut up, snark, flags."),
    Category("memory",    "💾 Memory",            "Facts, summaries, embeddings."),
    Category("chats",     "🌐 Chats & messaging", "List, pick, send, delete, silent."),
    Category("ai_admin",  "🎛 AI providers",       "Switch text providers + models."),
    Category("debug",     "🛠 Debug & status",     "Diagnostics, logs, toggles."),
)


# ─────────────────────────── COMMAND CATALOG ──────────────────────────────
# Keep slugs ASCII-only and unique. Slug = command name minus the slash, with
# any disambiguating suffix tacked on (e.g. ``ai_provider_admin``).
COMMANDS: tuple[Command, ...] = (
    # ── Basics ───────────────────────────────────────────────────────────
    Command("start",        "basics", "/start",
            "/start", "Greeting + a pointer to /help."),
    Command("help",         "basics", "/help",
            "/help", "Public command index."),
    Command("get_chat_id",  "basics", "/get_chat_id",
            "/get_chat_id", "Echo this chat's id."),
    Command("chat_config",  "basics", "/chat_config",
            "/chat_config [field] [value]",
            "Show or change this chat's settings (slash form)."),
    Command("config",       "basics", "/config",
            "/config",
            "Inline-keyboard settings wizard for this chat.",
            action="mgm:chats:config"),

    # ── AI ───────────────────────────────────────────────────────────────
    Command("a",            "ai", "/a, /askai, /ask",
            "/a <question>",
            "Quick AI answer (no memory write)."),
    Command("aigen",        "ai", "/aigen, /generate",
            "/aigen <prompt>",
            "Generate an image."),
    Command("aiedit",       "ai", "/aiedit",
            "/aiedit <prompt>",
            "Edit a replied-to image (placeholder)."),
    Command("aivar",        "ai", "/aivar",
            "/aivar",
            "Variation of a replied-to image (placeholder)."),
    Command("aitranslate",  "ai", "/aitranslate",
            "/aitranslate",
            "Translate a replied-to voice note."),
    Command("catfact",      "ai", "/catfact",
            "/catfact", "Dubious cat fact."),
    Command("beneficiality","ai", "/beneficiality",
            "/beneficiality",
            "Score whether I'd butt in right now."),
    Command("whatdid",      "ai", "/whatdid",
            "/whatdid @user",
            "Summarize what they've been up to."),
    Command("tldr",         "ai", "/tldr",
            "/tldr [duration]",
            "Summarize the recent chat (default 24h)."),
    Command("mood",         "ai", "/mood",
            "/mood",
            "Current mood + word of the day."),
    Command("haiku",        "ai", "/haiku",
            "/haiku", "Haiku about the recent chat."),
    Command("this_or_that", "ai", "/this_or_that",
            "/this_or_that A | B",
            "I decide, dramatically."),
    Command("echo_cmd",     "ai", "/echo",
            "/echo @user [topic]",
            "Mimic that user's style."),
    Command("roast",        "ai", "/roast",
            "/roast @user", "Toast that user."),
    Command("compliment",   "ai", "/compliment",
            "/compliment @user", "Compliment that user."),
    Command("lyric",        "ai", "/lyric",
            "/lyric <line>",
            "I confidently mishear the lyric."),
    Command("meme",         "ai", "/meme",
            "/meme top text | bottom text",
            "Generate a meme image."),
    Command("ether",        "ai", "/ether",
            "/ether <text> | /ether (reply to voice)",
            "Transmit a message as staticky shortwave-voice into another "
            "ether-tuned chat."),

    # ── Duckhunt ─────────────────────────────────────────────────────────
    Command("duckhunt",     "duckhunt", "/duckhunt",
            "/duckhunt", "Spawn a duck in this chat (if enabled)."),
    Command("quackflag",    "duckhunt", "/quackflag",
            "/quackflag", "Is there an active duck?"),
    Command("duckstats",    "duckhunt", "/duckstats",
            "/duckstats", "Leaderboard for this chat."),
    Command("duckfriends",  "duckhunt", "/duckfriends",
            "/duckfriends",
            "Your roster of befriended ducks here."),
    Command("duckname",     "duckhunt", "/duckname",
            "/duckname <id> <name>",
            "Name one of your befriended ducks."),
    Command("ducknames",    "duckhunt", "/ducknames",
            "/ducknames [page]",
            "Every named duck across every chat (paginated)."),
    Command("global_leaderboard", "duckhunt", "/global_leaderboard",
            "/global_leaderboard",
            "Duck leaderboard across all chats."),
    Command("quack_chat",   "duckhunt", "/quack_chat",
            "/quack_chat",
            "Admin: spawn a duck in a chat (picker).",
            action="mgm:duck:spawn"),
    Command("quack_all",    "duckhunt", "/quack_all",
            "/quack_all",
            "Admin: spawn a duck in every duckhunt-enabled chat.",
            action="mgm:duck:spawnall"),
    Command("duckstats_reset", "duckhunt", "/duckstats_reset",
            "/duckstats_reset",
            "Admin: wipe one (or all) user's duck_stats counters in a chat.",
            action="mgm:duck:reset"),
    Command("duckstats_edit",  "duckhunt", "/duckstats_edit",
            "/duckstats_edit",
            "Admin: edit a user's duck_stats fields (+/- buttons).",
            action="mgm:duck:edit"),

    # ── Quotes & lore ────────────────────────────────────────────────────
    Command("quote",        "quotes", "/quote",
            "/quote (reply to save) | /quote (alone for random)",
            "Save a replied-to message, or surface a random saved one."),
    Command("quotes",       "quotes", "/quotes",
            "/quotes", "List recent quotes."),
    Command("unquote",      "quotes", "/unquote",
            "/unquote <id>", "Delete a saved quote."),
    Command("catchphrases", "quotes", "/catchphrases",
            "/catchphrases @user",
            "Their repeated phrases."),
    Command("lexicon",      "quotes", "/lexicon",
            "/lexicon @user", "Their top words."),
    Command("heatmap",      "quotes", "/heatmap",
            "/heatmap",
            "This chat's activity by hour-of-day."),
    Command("whoslurking",  "quotes", "/whoslurking",
            "/whoslurking",
            "Users silent for more than 7 days."),
    Command("karma",        "quotes", "/karma",
            "/karma",
            "Chat karma leaderboard (👍/👎 reactions grant/dock)."),

    # ── Reminders & dates ────────────────────────────────────────────────
    Command("remind",       "reminders", "/remind",
            "/remind <duration> <text>",
            "Ex: /remind 1h30m feed the cat."),
    Command("birthday",     "reminders", "/birthday",
            "/birthday MM-DD",
            "Set your birthday."),
    Command("anniversary",  "reminders", "/anniversary",
            "/anniversary <name> MM-DD-YYYY",
            "Mark a chat date."),
    Command("dates",        "reminders", "/dates",
            "/dates", "List all tracked dates here."),
    Command("poll",         "reminders", "/poll",
            "/poll Q | A | B | …",
            "Create a poll."),

    # ── Anonymous (DM only) ──────────────────────────────────────────────
    Command("confess",      "dm", "/confess",
            "/confess <text>",
            "Submit an anonymous confession (DM only)."),

    # ── Moderation ───────────────────────────────────────────────────────
    Command("shutup",       "mod", "/shutup",
            "/shutup @user [duration]",
            "Bot ignores them."),
    Command("unshutup",     "mod", "/unshutup",
            "/unshutup @user", "Release the shutup flag."),
    Command("snark_at",     "mod", "/snark_at",
            "/snark_at @user",
            "Extra snark toward them."),
    Command("unsnark",      "mod", "/unsnark",
            "/unsnark @user", "Release the snark flag."),
    Command("ungrudge",     "mod", "/ungrudge",
            "/ungrudge @user", "Release the auto-grudge flag."),
    Command("flags",        "mod", "/flags",
            "/flags", "Active flags in this chat."),

    # ── Memory / embeddings (admin) ─────────────────────────────────────
    Command("memory_facts", "memory", "/memory_facts",
            "/memory_facts [chat_id]",
            "Browse stored facts for a chat.",
            action="mgm:memory:facts"),
    Command("memory_facts_all", "memory", "/memory_facts_all",
            "/memory_facts_all",
            "Every fact across every chat."),
    Command("memory_forget","memory", "/memory_forget",
            "/memory_forget <fact_id>",
            "Delete a single fact."),
    Command("memory_wipe",  "memory", "/memory_wipe",
            "/memory_wipe [chat_id] [facts]",
            "Erase a chat's conversation memory (messages + summary + "
            "embeddings). Add ``facts`` to also clear durable facts."),
    Command("memory_stats", "memory", "/memory_stats",
            "/memory_stats",
            "Per-chat memory diagnostics (picker).",
            action="mgm:memory:stats"),
    Command("memory_summary", "memory", "/memory_summary",
            "/memory_summary",
            "Latest stored summary (picker).",
            action="mgm:memory:summary"),
    Command("memory_summarize_now", "memory", "/memory_summarize_now",
            "/memory_summarize_now",
            "Force a summary + fact-extraction pass (picker).",
            action="mgm:memory:force"),
    Command("memory_search","memory", "/memory_search",
            "/memory_search [chat_id] <query>",
            "Semantic search the embedding store."),

    # ── Chats & messaging (admin) ────────────────────────────────────────
    Command("list_chat_ids","chats", "/list_chat_ids",
            "/list_chat_ids", "List all known chats.",
            action="mgm:chats:list"),
    Command("pick_chat",    "chats", "/pick_chat",
            "/pick_chat", "Picker that returns a chat id.",
            action="mgm:chats:pick"),
    Command("config_for",   "chats", "/config_for",
            "/config_for [chat_id]",
            "Open the /config wizard for any chat from DM.",
            action="mgm:chats:config"),
    Command("send_message", "chats", "/send_message",
            "/send_message <chat_id> <text>",
            "Send a message as the bot."),
    Command("delete_msg",   "chats", "/delete_msg",
            "/delete_msg [chat_id]",
            "Pick a recent bot message to delete."),
    Command("delete_last",  "chats", "/delete_last",
            "/delete_last [chat_id] [N]",
            "Delete the last N (≤ 20) bot messages in a chat."),
    Command("silenced_chats", "chats", "/silenced_chats",
            "/silenced_chats",
            "Show the admin-only 'silent' override list.",
            action="mgm:chats:silenced"),
    Command("silent_chat",  "chats", "/silent_chat",
            "/silent_chat <chat_id>",
            "Add a chat to the silenced set."),
    Command("unsilent_chat","chats", "/unsilent_chat",
            "/unsilent_chat <chat_id>",
            "Remove a chat from the silenced set."),

    # ── AI providers / persona (admin) ───────────────────────────────────
    Command("ai_provider",  "ai_admin", "/ai_provider",
            "/ai_provider show|claude|openai",
            "Show or switch the text-completion provider.",
            action="mgm:ai:show"),
    Command("ai_model",     "ai_admin", "/ai_model",
            "/ai_model show | [provider] <model_id>",
            "Show or switch the active text model."),
    Command("master_prompt","ai_admin", "/master_prompt",
            "/master_prompt show | set <text> | setfile | reset",
            "Inspect or override the global persona prompt."),

    # ── Debug & status (admin) ───────────────────────────────────────────
    Command("debug_help",   "debug", "/debug_help",
            "/debug_help", "Index of debug-only commands."),
    Command("debug_status", "debug", "(status)",
            "(panel)",
            "Diagnostics: provider, models, chat/message/fact counters, "
            "pgvector availability.",
            action="mgm:debug:status"),
    Command("debug_logs",   "debug", "/logs",
            "/logs [N] [filter]",
            "Tail the bot's program logs.",
            action="mgm:debug:logs"),
    Command("debug_cost",   "debug", "/cost",
            "/cost [chat_id]",
            "AI spend (last 7 days).",
            action="mgm:debug:cost"),
    Command("debug_cmdlog", "debug", "/cmdlog",
            "/cmdlog",
            "Command audit log from the DB.",
            action="mgm:debug:cmdlog"),
    Command("debug_toggles","debug", "/debug_toggle",
            "/debug_toggle [name] [on|off]",
            "Admin-scoped duckhunt cheats: always_hit, always_miss, "
            "always_pass_challenge, always_fail_challenge, "
            "always_refuse_bef, bypass_cooldowns.",
            action="mgm:debug:toggles"),
    Command("debug_captcha","debug", "/debug_captcha",
            "/debug_captcha", "Force a captcha challenge here."),
    Command("debug_challenge","debug", "/debug_challenge",
            "/debug_challenge",
            "Random bef challenge (captcha|trivia|recipe)."),
    Command("debug_trivia", "debug", "/debug_trivia",
            "/debug_trivia", "Force a trivia challenge."),
    Command("debug_recipe", "debug", "/debug_recipe",
            "/debug_recipe", "Force a recipe challenge."),
    Command("debug_duck",   "debug", "/debug_duck",
            "/debug_duck", "Alias for /duckhunt (force-spawn here)."),
    Command("debug_sharephoto","debug", "/debug_sharephoto",
            "/debug_sharephoto",
            "Force Dale to generate + post a photo now."),
    Command("debug_ether",  "debug", "/debug_ether",
            "/debug_ether",
            "Force one ether broadcast right now (needs ≥ 2 ether-enabled chats)."),
    Command("debug_clear_challenge","debug", "/debug_clear_challenge",
            "/debug_clear_challenge [chat_id]",
            "Clear stuck bef challenge(s) in a chat."),
    Command("debug_clear_duck","debug", "/debug_clear_duck",
            "/debug_clear_duck",
            "Force-resolve a chat's active duck (picker).",
            action="mgm:debug:cleard"),
    Command("debug_persona","debug", "/debug_persona",
            "/debug_persona",
            "Show the exact system prompt this chat resolves to + which "
            "layer (master_prompt / persona_custom / NEUTRAL) won."),
    Command("ether_status", "debug", "/ether_status",
            "/ether_status",
            "Which interference source the last /ether used + cache state."),
    Command("ether_refresh","debug", "/ether_refresh",
            "/ether_refresh",
            "Drop the live shortwave cache; next /ether refetches."),
)


def categories_in_order() -> tuple[Category, ...]:
    """The order categories should appear in the /manage top menu."""
    return CATEGORIES


def commands_in_category(category: str) -> tuple[Command, ...]:
    """Every command tagged ``category``, in declaration order."""
    return tuple(c for c in COMMANDS if c.category == category)


def command_by_slug(slug: str) -> Command | None:
    """O(n) lookup is fine — catalog is <200 rows. Returns None if absent."""
    for c in COMMANDS:
        if c.slug == slug:
            return c
    return None


def category_by_key(key: str) -> Category | None:
    for cat in CATEGORIES:
        if cat.key == key:
            return cat
    return None


def all_action_targets() -> tuple[str, ...]:
    """Every distinct ``action`` value referenced by a catalog row.

    Used by the leaf-manifest test to confirm wired actions resolve to
    real mgm: leaves so we don't ship a button that goes nowhere.
    """
    return tuple(sorted({c.action for c in COMMANDS if c.action}))
