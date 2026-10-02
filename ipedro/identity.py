"""Who this bot is: what it's called, the names it answers to, and whether
it carries Dale's own flourishes.

One deployment used to mean one bot, and that bot was Dale — so his names
were baked into the addressing regex, his catchphrases into canned replies,
his GIFs into reflexes. Running other bots from the same code needs all of
that to come from configuration instead.

Unset means Dale, exactly as before: DALE reproduces the hardcoded
patterns byte for byte, so a deployment that sets nothing behaves as it
always has. A configured bot gets a name regex built from its own aliases,
"bad <name>" rebukes for each, and none of Dale's flourishes (his "rusty
shackleford" catchphrases, his GIF library, his /start blurb) — a bot named
Hank shouldn't introduce itself as Rusty Shackleford.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Identity:
    name: str                       # what greetings and the classifier call it
    primary_alias: str              # the one name used in help text examples
    name_re: re.Pattern             # "is this message addressing me by name"
    names_pattern: str              # its names as a bare alternation, for
                                    # other detectors (insults) to embed
    rebuke_phrases: frozenset[str]  # whole messages that delete its reply
    dale_flavor: bool               # Dale's catchphrases, GIFs, /start blurb


# Dale's names, as they've always been: the current persona (who goes by
# Rusty Shackleford when he thinks he's being watched — the chat's display
# name, so people type "rusty" far more than the full alias) plus the legacy
# Boomhauer / Dude / Pedro aliases, so people who knew earlier personas keep
# getting an answer. Bare first names are allowed even though they're
# common: the bot can handle the occasional false hit.
_DALE_NAME_RE = re.compile(
    r"\bdale\s+gribble\b"
    r"|\brusty\s+shackleford\b"
    r"|\bshackleford\b"
    r"|\brusty\b"
    r"|\bidale\b"
    r"|\bdale\b"
    r"|\bboomhauer\b"
    r"|\bboomhaur\b"           # common misspelling
    r"|\bthe\s+dude\b"
    r"|\bduder(ino)?\b"
    r"|\bel\s+duderino\b"
    r"|\bhis\s+dudeness\b"
    r"|\bpedro\b",
    re.IGNORECASE,
)

DALE = Identity(
    name="Dale",
    primary_alias="dale",
    name_re=_DALE_NAME_RE,
    # Wider than the addressing regex on one point: bare "dude", which the
    # insult detector has always counted ("dude you're useless") but which
    # is too common an interjection to count as calling him by name.
    names_pattern=(
        r"dale(?:\s+gribble)?|idale|rusty(?:\s+shackleford)?|shackleford"
        r"|boomhaue?r|the\s+dude|dude|duder(?:ino)?|el\s+duderino|pedro"
    ),
    rebuke_phrases=frozenset({
        "bad bot", "bad pedro", "bad dude", "bad duder", "bad dale",
        "bad boomhauer", "bad rusty",
    }),
    dale_flavor=True,
)


def _alias_pattern(alias: str) -> str:
    """One alias as a regex fragment: literal, with any run of spaces
    matching any run of whitespace ("hank  hill" still says Hank Hill)."""
    return r"\s+".join(re.escape(part) for part in alias.split())


@lru_cache(maxsize=16)
def _build(name: str, aliases: tuple[str, ...], dale_flavor: bool) -> Identity:
    names_pattern = "|".join(_alias_pattern(a) for a in aliases)
    return Identity(
        name=name,
        primary_alias=aliases[0],
        # Lookarounds rather than \b: an alias may start or end in
        # punctuation ("mr. t", "c-3po."), where \b would need a word
        # character on the far side and never match.
        name_re=re.compile(rf"(?<!\w)(?:{names_pattern})(?!\w)", re.IGNORECASE),
        names_pattern=names_pattern,
        rebuke_phrases=frozenset({"bad bot"} | {f"bad {a}" for a in aliases}),
        dale_flavor=dale_flavor,
    )


def from_settings(settings) -> Identity:
    """The identity a Settings describes. Fields left unset mean Dale —
    which is what a bare deployment (or a test stub with none of these
    fields) has always been."""
    name = (getattr(settings, "bot_name", "") or "Dale").strip()
    raw = getattr(settings, "bot_aliases", "") or ""
    flavor = (getattr(settings, "bot_flavor", "") or "dale").strip().lower()
    aliases = tuple(dict.fromkeys(
        a.strip().lower() for a in raw.split(",") if a.strip()
    ))
    if not aliases and name == "Dale" and flavor == "dale":
        return DALE
    return _build(name, aliases or (name.lower(),), flavor == "dale")


def starting_persona(settings) -> str | None:
    """The persona text a deployment starts with; None means Dale's own.
    A bot that isn't Dale and was given no persona is just itself, by
    name — never Dale by default."""
    persona = (getattr(settings, "bot_persona", None) or "").strip()
    if persona:
        return persona
    ident = from_settings(settings)
    return None if ident.dale_flavor else f"You are {ident.name}."
