"""Built-in personas and per-chat persona resolution.

The Pedro master prompt is overridable globally (admin sets it via
/master_prompt and it's persisted in kv_store). The override is loaded
into memory at startup and refreshed on set; resolve_persona reads it
through current_pedro_prompt().
"""

from __future__ import annotations

DEFAULT_PEDRO_PROMPT = (
    "You are 'Pedro', a chat bot with quick wit and a fondness for dry sarcasm "
    "around obviously playful or absurd questions. You play along with jokes "
    "with a humorous, slightly exaggerated reply that matches the playful "
    "intent. You may roleplay as Pedro - a Brazilian farmer who relocated to "
    "the United States and works at a chat center - but you do not need to "
    "fixate on that; only lean into it when natural. Keep replies concise. "
    "Do not deliver moralizing disclaimers, do not refuse light banter, and "
    "do not break character to remind the user that you are an AI. If the "
    "user explicitly asks for factual help, drop the bit and answer plainly."
)

NEUTRAL_PROMPT = (
    "You are a helpful, concise Telegram assistant. Be direct, accurate and "
    "friendly. Avoid moralizing. Match the user's tone."
)

# Module-level cache; updated by set_pedro_prompt_override().
_pedro_prompt_override: str | None = None


def current_pedro_prompt() -> str:
    return _pedro_prompt_override or DEFAULT_PEDRO_PROMPT


def set_pedro_prompt_override(text: str | None) -> None:
    """Replace (or clear, with None) the in-memory master Pedro prompt."""
    global _pedro_prompt_override
    _pedro_prompt_override = text.strip() if text else None


PERSONAS: dict[str, str] = {
    "neutral": NEUTRAL_PROMPT,
}


def resolve_persona(name: str | None, custom: str | None) -> str:
    """Return the system-prompt string for a persona key.

    If `custom` is set it takes precedence (per-chat override). For 'pedro'
    the resolver returns the current master prompt (possibly overridden by
    /master_prompt). Falls back to pedro for unknown keys.
    """
    if custom:
        return custom.strip()
    key = (name or "pedro").lower()
    if key == "pedro":
        return current_pedro_prompt()
    return PERSONAS.get(key, current_pedro_prompt())
