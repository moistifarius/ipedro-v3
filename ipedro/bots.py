"""The other bots the owner runs alongside Dale: the registry, and the
rules for turning a registry row into a running process.

Each is a genuinely separate Telegram bot — its own account (made in
@BotFather), its own process, its own database — running this same code
under its own identity (ipedro/identity.py). Dale's database holds the
registry; the owner adds bots from DM with /newbot (handlers/bots.py); a
separate supervisor process (ipedro/supervisor.py) reads the registry,
creates each bot's database and keeps its process running.

Trust: only the owner can add or change a bot, a token never leaves this
table except into that one bot's own environment, and nothing typed in a
chat ever reaches a SQL identifier — a bot's database name is built from
the numeric id Telegram itself reports for the token.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from ipedro.kv import kv_get, kv_set

# BotFather tokens: "<numeric bot id>:<35-ish url-safe chars>".
TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{30,}\b")
# The same, anywhere in a line — including mid-URL ("/bot123:AAH…/getMe"),
# where the \b above wouldn't fire. For scrubbing, not for parsing.
_TOKEN_ANYWHERE_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
_DB_NAME_RE = re.compile(r"^ipedro_bot_\d+$")
_NAME_MAX = 64

HEARTBEAT_KEY = "supervisor_heartbeat"
CHILD_DB_POOL_MAX = 4

# What a bot must never inherit from the supervisor's environment, which is
# Dale's: his token and database (set per bot below), his identity (ditto),
# the right to manage bots (Dale's DM is the one place that happens), and
# the /evolve token (a bot filing change requests against the shared code
# would be a second door into self-modification).
_NOT_INHERITED = frozenset({
    "TELEGRAM_BOT_TOKEN", "DATABASE_URL",
    "BOT_NAME", "BOT_ALIASES", "BOT_FLAVOR", "BOT_PERSONA",
    "MANAGES_BOTS", "EVOLVE_GITHUB_TOKEN", "HUB_DATABASE_URL",
})

USAGE = (
    "Usage: /newbot <token> <Name>[, other names it answers to]\n"
    "On the lines after that, describe it in a few words: who it is, who "
    "it loves or can't stand. I'll write its persona from that and from "
    "what the chats remember about whoever and whatever you mention.\n\n"
    "Make the bot first: @BotFather → /newbot gives you the token. Then "
    "turn its Group Privacy off (@BotFather → /mybots → Bot Settings) so "
    "it can hear a group, not just commands and replies."
)


class NewBotError(ValueError):
    """What's wrong with a /newbot command, in words for the owner."""


@dataclass(frozen=True)
class NewBotRequest:
    token: str = field(repr=False)
    name: str
    aliases: str              # comma-separated, as BOT_ALIASES takes them
    description: str | None = None   # the owner's few words; the persona is written from it


@dataclass(frozen=True)
class BotRow:
    id: int
    telegram_id: int
    username: str
    name: str
    aliases: str
    status: str
    # Never in a repr: a logged row must not carry the token with it.
    token: str = field(repr=False)
    persona: str | None = field(default=None, repr=False)
    description: str | None = field(default=None, repr=False)
    running: bool = False
    started_at: datetime | None = None
    restarts: int = 0
    last_exit: str | None = None
    last_seen_at: datetime | None = None

    @classmethod
    def from_record(cls, r) -> BotRow:
        return cls(
            id=r["id"], telegram_id=r["telegram_id"], username=r["username"],
            name=r["name"], aliases=r["aliases"], status=r["status"],
            token=r["token"], persona=r["persona"], running=r["running"],
            started_at=r["started_at"], restarts=r["restarts"],
            last_exit=r["last_exit"], last_seen_at=r["last_seen_at"],
            description=r["description"] if "description" in r.keys() else None,
        )


def scrub(line: str, token: str = "") -> str:
    """A line with any bot token in it masked. A bot's output reaches the
    supervisor's log and, through last_exit, the owner's /bots — neither
    should ever carry a token."""
    if token:
        line = line.replace(token, "<token>")
    return _TOKEN_ANYWHERE_RE.sub("<token>", line)


def contains_token(text: str | None) -> bool:
    return bool(text) and TOKEN_RE.search(text) is not None


def parse_newbot(text: str) -> NewBotRequest:
    """'/newbot <token> <Name>[, alias, ...]' on the first line; everything
    after the first line describes it (ipedro/persona_gen.py writes the
    persona from that)."""
    first, _, rest = (text or "").partition("\n")
    parts = first.split(None, 2)
    if len(parts) < 3:
        raise NewBotError(USAGE)
    token = parts[1].strip()
    if not TOKEN_RE.fullmatch(token):
        raise NewBotError(
            "That doesn't look like a bot token. @BotFather gives you one "
            "shaped like 123456789:AAH... — paste it whole.\n\n" + USAGE
        )
    names = [n.strip() for n in parts[2].split(",") if n.strip()]
    if not names:
        raise NewBotError(USAGE)
    if any(len(n) > _NAME_MAX for n in names):
        raise NewBotError(f"Names top out at {_NAME_MAX} characters.")
    aliases = ", ".join(dict.fromkeys(n.lower() for n in names))
    return NewBotRequest(
        token=token, name=names[0], aliases=aliases,
        description=" ".join(rest.split()) or None,
    )


def db_name_for(telegram_id: int) -> str:
    return f"ipedro_bot_{int(telegram_id)}"


def is_safe_db_name(name: str) -> bool:
    return bool(_DB_NAME_RE.fullmatch(name or ""))


def child_database_url(base_url: str, db_name: str) -> str:
    """Dale's DATABASE_URL, pointed at another database on the same server."""
    if not is_safe_db_name(db_name):
        raise ValueError(f"refusing database name {db_name!r}")
    return urlunsplit(urlsplit(base_url)._replace(path="/" + db_name))


def child_env(
    base: Mapping[str, str], row: BotRow, base_database_url: str,
) -> dict[str, str]:
    """The environment one bot runs with: everything Dale's has (API keys,
    timezone, models) minus what's his alone, plus its own token,
    database and identity."""
    env = {k: v for k, v in base.items() if k.upper() not in _NOT_INHERITED}
    env.update({
        "TELEGRAM_BOT_TOKEN": row.token,
        "DATABASE_URL": child_database_url(
            base_database_url, db_name_for(row.telegram_id),
        ),
        "BOT_NAME": row.name,
        "BOT_ALIASES": row.aliases,
        "BOT_FLAVOR": "plain",
        "MANAGES_BOTS": "false",
        # Small pools: each bot adds a pool, a hub connection and a listener
        # to one Postgres (default max_connections 100).
        "DB_POOL_MAX": str(CHILD_DB_POOL_MAX),
        # Dale's own database is the hub where the bots hear each other.
        "HUB_DATABASE_URL": base_database_url,
    })
    if row.persona:
        env["BOT_PERSONA"] = row.persona
    return env


def fingerprint(row: BotRow) -> str:
    """Changes whenever anything the bot's process was started with does,
    so the supervisor knows to restart it."""
    raw = "\x00".join((row.token, row.name, row.aliases, row.persona or ""))
    return hashlib.sha256(raw.encode()).hexdigest()


# ── the registry ─────────────────────────────────────────────────────────────

async def register(
    db, req: NewBotRequest, *, telegram_id: int, username: str,
    created_by: int, persona: str | None,
) -> BotRow | None:
    """Add a bot, or bring back a removed one with these settings. None
    when it's already registered and not removed — the owner stops or
    removes it first, rather than a second /newbot silently replacing it."""
    row = await db.fetchrow(
        """
        INSERT INTO bot_registry (telegram_id, username, name, aliases,
                                  persona, description, token, created_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (telegram_id) DO UPDATE SET
            username = EXCLUDED.username, name = EXCLUDED.name,
            aliases = EXCLUDED.aliases, persona = EXCLUDED.persona,
            description = EXCLUDED.description,
            token = EXCLUDED.token, created_by = EXCLUDED.created_by,
            status = 'active', updated_at = NOW()
        WHERE bot_registry.status = 'removed'
        RETURNING *
        """,
        telegram_id, username, req.name, req.aliases, persona,
        req.description, req.token, created_by,
    )
    return BotRow.from_record(row) if row else None


async def set_persona(
    db, bot_id: int, persona: str, description: str | None,
) -> BotRow | None:
    """A new persona for a bot; the supervisor restarts it with this one."""
    row = await db.fetchrow(
        "UPDATE bot_registry SET persona = $2, description = $3, "
        "updated_at = NOW() WHERE id = $1 RETURNING *",
        bot_id, persona, description,
    )
    return BotRow.from_record(row) if row else None


async def find_by_telegram_id(db, telegram_id: int) -> BotRow | None:
    row = await db.fetchrow(
        "SELECT * FROM bot_registry WHERE telegram_id = $1", telegram_id,
    )
    return BotRow.from_record(row) if row else None


async def list_bots(db, *, include_removed: bool = False) -> list[BotRow]:
    rows = await db.fetch(
        "SELECT * FROM bot_registry"
        + ("" if include_removed else " WHERE status <> 'removed'")
        + " ORDER BY id"
    )
    return [BotRow.from_record(r) for r in rows]


async def wanted(db) -> list[BotRow]:
    """What should be running right now."""
    rows = await db.fetch(
        "SELECT * FROM bot_registry WHERE status = 'active' ORDER BY id"
    )
    return [BotRow.from_record(r) for r in rows]


async def resolve(db, key: str) -> list[BotRow]:
    """Bots matching how the owner named one: '#3' / '3', '@HankBot', or a
    name it answers to. Removed bots never match."""
    key = (key or "").strip()
    bots = await list_bots(db)
    if key.lstrip("#").isdigit():
        return [b for b in bots if b.id == int(key.lstrip("#"))]
    k = key.lstrip("@").lower()
    by_username = [b for b in bots if b.username.lower() == k]
    if by_username:
        return by_username
    return [
        b for b in bots
        if k == b.name.lower()
        or k in (a.strip() for a in b.aliases.split(","))
    ]


async def set_status(db, bot_id: int, status: str) -> BotRow | None:
    if status not in ("active", "stopped", "removed"):
        raise ValueError(status)
    row = await db.fetchrow(
        "UPDATE bot_registry SET status = $2, updated_at = NOW() "
        "WHERE id = $1 RETURNING *",
        bot_id, status,
    )
    return BotRow.from_record(row) if row else None


async def report(
    db, bot_id: int, *, running: bool, started_at: datetime | None = None,
    restarts: int | None = None, last_exit: str | None = None,
) -> None:
    """The supervisor's side: what it actually sees."""
    await db.execute(
        """
        UPDATE bot_registry SET
            running = $2,
            started_at = COALESCE($3, started_at),
            restarts = COALESCE($4, restarts),
            last_exit = COALESCE($5, last_exit),
            last_seen_at = NOW()
        WHERE id = $1
        """,
        bot_id, running, started_at, restarts, last_exit,
    )


async def beat(db) -> None:
    await kv_set(db, HEARTBEAT_KEY, datetime.now(timezone.utc).isoformat())


async def supervisor_last_seen(db) -> datetime | None:
    raw = await kv_get(db, HEARTBEAT_KEY)
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None
