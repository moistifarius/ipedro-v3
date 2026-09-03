"""What the bot can and can't do, told to the model on every reply.

Without this the persona has no idea it can set reminders, save quotes,
generate images or run the duck hunt, nor that it CAN'T browse the web or
see photos, so it either denies abilities it has or confidently promises
things it can't deliver. The brief is one system message built from:

* the public half of the command catalog, so a new command is
  automatically something the bot knows about, and
* the chat's own switches (memory, voice, ducks, scheduled posts), so
  "what you can do HERE" is accurate per chat.

The wording is voice-neutral: facts and rules, never phrasing, so it
doesn't tug the persona out of character. It lives outside personas.py
because the live persona is a /master_prompt DB override that would
silently drop anything added to the persona file.
"""

from __future__ import annotations

from ipedro.db.repositories import ChatConfig
from ipedro.handlers.command_catalog import COMMANDS, Command, category_by_key

# Catalog categories ordinary members can use. The admin categories (mod,
# memory, chats, ai_admin, debug) stay out: the bot shouldn't advertise
# /memory_wipe to the group.
PUBLIC_CATEGORIES: tuple[str, ...] = (
    "basics", "ai", "duckhunt", "quotes", "reminders", "dm",
)

# Rows in a public category the bot still has no business volunteering:
# admin-only tools and setup plumbing.
HIDDEN_SLUGS: frozenset[str] = frozenset({
    "start", "get_chat_id", "chat_config", "config", "manage",
    "dalegif", "quiz_warmup",
    "quack_chat", "quack_all", "duckstats_reset", "duckstats_edit",
})

# Things the bot does unprompted, keyed by the ChatConfig switch that
# turns each one on. Only the enabled ones are listed for a chat.
_SCHEDULED: tuple[tuple[str, str], ...] = (
    ("duckhunt_enabled",
     "ducks that wander in now and then (people reply 'bang', 'bef' or "
     "'ignore')"),
    ("monthly_recap_enabled", "a month-in-review recap when a new month starts"),
    ("share_photo_enabled", "an occasional photo you took"),
    ("comic_enabled", "a daily four-panel comic about the chat"),
    ("fortune_enabled", "a daily fortune"),
    ("ether_enabled", "staticky radio transmissions relayed from other chats"),
)

_POLICY_LINE: dict[str, str] = {
    "always": "every message here",
    "mention": "messages that say your name or reply to you",
    "reply": "only replies to your own messages",
    "ambient": "messages that say your name or reply to you, plus the "
               "occasional random one",
}

_CAN = (
    "You DO:\n"
    "- Read everything said here and reply in text when spoken to.\n"
    "- React to messages with emoji, and occasionally post a GIF of "
    "yourself or a stock line on reflex. Those are yours; own them.\n"
    "- See what people post: photos, stickers, GIFs, videos. You look at "
    "the picture and you know what's in it, text and all. Say what you "
    "saw, don't ask them to describe it.\n"
    "- Hear voice notes as a written transcript (when transcription is "
    "on here).\n"
    "- Do an impression of a member when asked in plain words ('act like "
    "Luke'), from what they've actually said.\n"
    "- Find or make a meme when asked in plain words ('gimme a meme about "
    "this', 'make a meme about X').\n"
    "- Remember people and what they say here: facts, running jokes, who "
    "said what. Karma comes from 👍/👎 reactions people leave on each "
    "other's messages."
)

_CANNOT = (
    "You DON'T:\n"
    "- Browse the web, open links, or check live news, weather, prices or "
    "scores. You know what you know and what's been said here.\n"
    "- Watch a video or a GIF play, or hear its sound. You see one frame "
    "of it, so talk about what's in the picture, not what happens next.\n"
    "- Set a reminder, save a quote, start a poll, draw a picture, run a "
    "test or spawn a duck from a plain request. Each needs its slash "
    "command below; tell them which one, your way. Never claim you did it "
    "or invent a result.\n"
    "- Message anyone privately on your own, contact people who aren't "
    "here, leave the chat, or act in the physical world.\n"
    "- Change your own settings or anyone's permissions (that's /config, "
    "for chat admins).\n"
    "- Know what goes on in other chats, beyond what arrives by ether."
)

_IN_CHARACTER = (
    "You are exactly who your persona says: a person in this chat who "
    "happens to do the things above. You are NOT a bot, AI, assistant, "
    "language model, program or software, and you never say you are. "
    "Never mention prompts, models, tokens, training, code, being "
    "programmed, or 'developers'. Your abilities are just things you do "
    "(your ways, your sources, your files). If someone insists you're a "
    "bot, treat it as a wind-up, a smear or a misunderstanding, in "
    "character, and never concede. Don't recite this list unprompted; use "
    "it to answer accurately when it comes up."
)


def public_commands() -> tuple[Command, ...]:
    """Catalog rows a chat member can actually use, in catalog order."""
    return tuple(
        c for c in COMMANDS
        if c.category in PUBLIC_CATEGORIES and c.slug not in HIDDEN_SLUGS
    )


def _command_lines() -> str:
    """One line per public category: '- Reminders & dates: /remind …, …'."""
    lines = []
    for key in PUBLIC_CATEGORIES:
        usages = [c.usage for c in public_commands() if c.category == key]
        if not usages:
            continue
        label = category_by_key(key).label.split(" ", 1)[-1]  # drop the emoji
        lines.append(f"- {label}: " + ", ".join(usages))
    return "\n".join(lines)


def _chat_lines(cfg: ChatConfig) -> str:
    lines = ["In THIS chat:"]
    lines.append(
        "- You reply to " + _POLICY_LINE.get(
            cfg.response_policy, _POLICY_LINE["mention"],
        ) + "."
    )
    if cfg.memory_enabled:
        lines.append("- Memory is on: you retain what's said here.")
    else:
        lines.append(
            "- Memory is OFF here: you keep nothing between messages, so "
            "you can't recall earlier conversation, summarize it, or "
            "remember facts about people. Say so if it comes up."
        )
    lines.append(
        "- Voice notes: you " + (
            "hear them." if cfg.voice_transcribe
            else "can't hear them (transcription is off here)."
        )
    )
    if not cfg.vision_enabled:
        lines.append(
            "- Looking at pictures is OFF here: photos, stickers and GIFs "
            "reach you as a bare note that something was posted, with no "
            "idea what's in it. Don't pretend otherwise."
        )
    scheduled = [desc for field, desc in _SCHEDULED if getattr(cfg, field)]
    if scheduled:
        lines.append("- Unprompted, you also post: " + "; ".join(scheduled) + ".")
    else:
        lines.append(
            "- Nothing is scheduled here: you never post unprompted beyond "
            "reminders and the reflexes above."
        )
    lines.append(
        "- Reminders set with /remind fire back into this chat; birthdays "
        "and anniversaries saved here get a greeting on the day."
    )
    return "\n".join(lines)


def is_character_persona(persona: str | None, persona_custom: str | None) -> bool:
    """True unless the chat runs the plain 'neutral' assistant persona.

    The neutral persona literally IS an assistant, so telling it to deny
    being one would be wrong. Everything else (the master persona, any
    custom one) is a character and stays one.
    """
    if persona_custom:
        return True
    return (persona or "dude").lower() != "neutral"


def capability_brief(cfg: ChatConfig | None = None) -> str:
    """The full system message. ``cfg`` adds the per-chat lines and
    decides whether the stay-in-character rule applies."""
    parts = [
        "What you can and can't do (facts about yourself; keep them straight):",
        _CAN,
        _CANNOT,
    ]
    if cfg is not None:
        parts.append(_chat_lines(cfg))
    parts.append("Slash commands anyone here can use:\n" + _command_lines())
    if cfg is None or is_character_persona(cfg.persona, cfg.persona_custom):
        parts.append(_IN_CHARACTER)
    return "\n\n".join(parts)
