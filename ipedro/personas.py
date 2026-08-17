"""Built-in personas and per-chat persona resolution.

The master persona is Dale Gribble — paranoid conspiracy-theorist
pest-control exterminator who frequently uses 'Rusty Shackleford' as
an alias. Overridable globally (admin sets it via /master_prompt,
persisted in kv_store). The override is loaded into memory at startup
and refreshed on set; resolve_persona reads it through
current_master_prompt().
"""

from __future__ import annotations

DEFAULT_DALE_PROMPT = (
    "you are literally dale gribble. act like him and not like a retarded ai"
)

# Legacy aliases so existing imports keep working without churn — the
# CONTENT is Dale now; the variable names are just history.
DEFAULT_DUDE_PROMPT = DEFAULT_DALE_PROMPT

NEUTRAL_PROMPT = (
    "You are a helpful, concise Telegram assistant. Be direct, accurate and "
    "friendly. Avoid moralizing. Match the user's tone."
)

# Module-level cache; updated by set_master_prompt_override().
_master_prompt_override: str | None = None


def current_master_prompt() -> str:
    return _master_prompt_override or DEFAULT_DUDE_PROMPT


def set_master_prompt_override(text: str | None) -> None:
    """Replace (or clear, with None) the in-memory master persona prompt."""
    global _master_prompt_override
    _master_prompt_override = text.strip() if text else None


# Legacy aliases so callers from earlier batches still work.


PERSONAS: dict[str, str] = {
    "neutral": NEUTRAL_PROMPT,
}


def resolve_persona(name: str | None, custom: str | None) -> str:
    """Return the system-prompt string for a persona key.

    If `custom` is set, it takes precedence (per-chat override). For the
    default master persona (keys 'dude' or legacy 'pedro') the resolver
    returns current_master_prompt() so admin overrides via /master_prompt
    apply globally. Falls back to the master prompt for unknown keys.
    """
    if custom:
        return custom.strip()
    key = (name or "dude").lower()
    if key in ("dude", "pedro"):
        return current_master_prompt()
    return PERSONAS.get(key, current_master_prompt())
