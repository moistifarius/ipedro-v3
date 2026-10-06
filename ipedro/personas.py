"""Built-in personas and per-chat persona resolution.

The master persona is Dale Gribble — paranoid conspiracy-theorist
pest-control exterminator who frequently uses 'Rusty Shackleford' as
an alias. Overridable globally (admin sets it via /master_prompt,
persisted in kv_store). The override is loaded into memory at startup
and refreshed on set; resolve_persona reads it through
current_master_prompt().
"""

from __future__ import annotations

# The words are in prompts.py, which an /evolve change may edit unreviewed;
# choosing between them is here, which it may not.
from ipedro.prompts import DEFAULT_DALE_PROMPT, NEUTRAL_PROMPT

# Legacy aliases so existing imports keep working without churn — the
# CONTENT is Dale now; the variable names are just history.
DEFAULT_DUDE_PROMPT = DEFAULT_DALE_PROMPT

# Module-level cache; updated by set_master_prompt_override().
_master_prompt_override: str | None = None

# What the master persona falls back to with no /master_prompt override:
# Dale, unless this deployment is a different bot (settings.bot_persona,
# applied once at startup by set_default_prompt).
_default_prompt: str = DEFAULT_DALE_PROMPT


def default_prompt() -> str:
    return _default_prompt


def set_default_prompt(text: str | None) -> None:
    """Replace (or restore, with None) the persona this bot starts as."""
    global _default_prompt
    _default_prompt = text.strip() if text and text.strip() else DEFAULT_DALE_PROMPT


def current_master_prompt() -> str:
    return _master_prompt_override or _default_prompt


def set_master_prompt_override(text: str | None) -> None:
    """Replace (or clear, with None) the in-memory master persona prompt."""
    global _master_prompt_override
    _master_prompt_override = text.strip() if text else None


# Non-master personas, selected by chat_config.persona.
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
