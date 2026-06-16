"""Admin-only commands. All require private DM + admin user id."""

from __future__ import annotations

import io
import logging
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from aiogram.exceptions import TelegramBadRequest

from ipedro.auth import is_admin_user
from ipedro.bot_messages import (
    TrackedMessage, forget as forget_tracked, recent as recent_tracked,
)
from ipedro.duckhunt.debug_toggles import (
    DEBUG_TOGGLE_NAMES, all_for as debug_toggles_all_for, set_toggle as set_debug_toggle,
)
from ipedro.duckhunt.spawner import (
    build_quack_message, duckhunt_enabled_chat_ids, duckhunt_enabled_chats,
)
from ipedro.handlers.command_catalog import (
    CATEGORIES, COMMANDS,
    Command as CatalogCommand,
    categories_in_order, category_by_key, command_by_slug,
    commands_in_category,
)
from ipedro.handlers.common import require_admin
from ipedro.kv import kv_delete, kv_get, kv_set
from ipedro.logging_setup import recent_log_lines
from ipedro.memory.summarizer import force_summarize
from ipedro.memory.tokens import count_tokens
from ipedro.personas import (
    DEFAULT_DUDE_PROMPT, current_master_prompt, set_master_prompt_override,
)
from ipedro.runtime import Runtime
from ipedro.silenced_chats import (
    is_silenced, list_silenced, silence as silence_chat, unsilence as unsilence_chat,
)

# Admin-keyed stash for /memory_search: the user types `/memory_search <query>`
# (the query can't fit in a 64-byte callback_data alongside the chat id), so
# we park it here keyed by the admin's user_id and read it back when they
# click a chat in the picker.
_PENDING_SEARCH_QUERIES: dict[int, tuple[str, float]] = {}
_SEARCH_QUERY_TTL = 300.0  # seconds; safety in case the admin never clicks


def _stash_search_query(user_id: int, query: str) -> None:
    _PENDING_SEARCH_QUERIES[user_id] = (query, time.time())


def _pop_search_query(user_id: int) -> str | None:
    entry = _PENDING_SEARCH_QUERIES.pop(user_id, None)
    if entry is None:
        return None
    query, ts = entry
    if time.time() - ts > _SEARCH_QUERY_TTL:
        return None
    return query


# Upper bound on the byte size of an uploaded /master_prompt file. Anything
# bigger than this is almost certainly a mistake — the in-context budget
# tops out at a few thousand tokens (settings.context_max_tokens), so 64 KB
# of raw text already overshoots usable territory. We accept up to this and
# then warn separately if the token count exceeds the runtime budget.
_MASTER_PROMPT_FILE_MAX_BYTES = 64 * 1024

log = logging.getLogger(__name__)


async def _resolve_user_id(rt, chat_id: int, ref: str) -> int | None:
    """Resolve an admin-supplied user reference.

    Accepts numeric ('12345', '@12345'), bare or @-prefixed username, or
    bare display name. Resolution priority:
      1. Numeric → int() directly (no DB).
      2. users.username case-insensitive LIMIT 1.
      3. duck_stats.display_name case-insensitive scoped to chat_id LIMIT 1.
      4. None.
    """
    if ref is None:
        return None
    # Strip a SINGLE leading '@' so '@bob' looks like 'bob', but '@@bob'
    # becomes '@bob' (we then treat it as a username with a literal '@').
    stripped = ref[1:] if ref.startswith("@") else ref
    if not stripped:
        return None
    # Numeric → int() directly (no DB).
    if stripped.isdigit():
        try:
            return int(stripped)
        except ValueError:
            return None
    # Username lookup, case-insensitive.
    row = await rt.db.fetchrow(
        "SELECT user_id FROM users WHERE LOWER(username) = LOWER($1) LIMIT 1",
        stripped,
    )
    if row:
        return int(row["user_id"])
    # Display-name lookup scoped to the chat. Log a warning if multiple
    # display_name rows match — the picked row is arbitrary.
    count = await rt.db.fetchval(
        "SELECT COUNT(*) FROM duck_stats "
        "WHERE chat_id = $1 AND LOWER(display_name) = LOWER($2)",
        chat_id, stripped,
    )
    if count and int(count) > 1:
        log.warning(
            "Multiple display_name matches for %r in chat %s; picked arbitrary.",
            stripped, chat_id,
        )
    row = await rt.db.fetchrow(
        "SELECT user_id FROM duck_stats "
        "WHERE chat_id = $1 AND LOWER(display_name) = LOWER($2) LIMIT 1",
        chat_id, stripped,
    )
    if row:
        return int(row["user_id"])
    return None


def _chat_label(chat: dict) -> str:
    title = chat.get("title")
    if title:
        return f"{title} ({chat['type']})"[:60]
    return f"{chat['type']} {chat['chat_id']}"[:60]


# Sticky last-pick per admin user. Maps admin_user_id -> (chat_id, last_ts).
# A 10-minute TTL keeps it from getting stale.
_LAST_PICKED_CHAT: dict[int, tuple[int, float]] = {}
_LAST_PICKED_TTL = 600.0  # seconds

# Page size for paginated chat pickers.
_CHAT_PICKER_PAGE_SIZE = 20


def _expired(cmd: str) -> str:
    return f"Selection expired — re-run /{cmd} to start over."


def _parse_picker_cb(data: str) -> tuple[str, int] | None:
    """Parse `action:p:N` → ("page", N), or `action:N` → ("pick", N).
    Returns None on malformed input."""
    parts = data.split(":", 2)
    if len(parts) < 2:
        return None
    try:
        if len(parts) == 3 and parts[1] == "p":
            return ("page", int(parts[2]))
        return ("pick", int(parts[1]))
    except ValueError:
        return None


def _chat_picker(
    chats: list[dict],
    action: str,
    *,
    paginate: bool = False,
    page: int = 0,
    admin_user_id: int | None = None,
    home_to_manage: bool = False,
) -> InlineKeyboardMarkup | None:
    """Build a one-button-per-row picker. action is the callback prefix.

    Telegram caps callback_data at 64 bytes; the chat_id alone fits well
    inside that. When paginate=True and the chat list exceeds the page
    size, splits into pages with prev/next buttons. When admin_user_id is
    provided, prepends a "last pick" sticky row (if within TTL) on page 0.
    """
    if not chats:
        return None
    body_rows: list[list[InlineKeyboardButton]] = []
    paginated = paginate and len(chats) > _CHAT_PICKER_PAGE_SIZE
    if paginated:
        total_pages = (len(chats) + _CHAT_PICKER_PAGE_SIZE - 1) // _CHAT_PICKER_PAGE_SIZE
        page = max(0, min(page, total_pages - 1))
        start = page * _CHAT_PICKER_PAGE_SIZE
        sliced = chats[start:start + _CHAT_PICKER_PAGE_SIZE]
    else:
        total_pages = 1
        page = 0
        # Preserve original cap of 50 when not paginating.
        sliced = chats[:50]
    # Sticky last-pick row (only on page 0).
    if admin_user_id is not None and page == 0:
        entry = _LAST_PICKED_CHAT.get(admin_user_id)
        if entry is not None:
            pinned_id, pinned_ts = entry
            if time.time() - pinned_ts < _LAST_PICKED_TTL:
                pinned_chat = next(
                    (c for c in chats if c["chat_id"] == pinned_id), None,
                )
                if pinned_chat is not None:
                    body_rows.append([InlineKeyboardButton(
                        text=f"⭐ {_chat_label(pinned_chat)} (last pick)",
                        callback_data=f"{action}:{pinned_id}",
                    )])
    # Main body rows.
    for c in sliced:
        body_rows.append([InlineKeyboardButton(
            text=_chat_label(c), callback_data=f"{action}:{c['chat_id']}",
        )])
    # Pagination row, if applicable.
    if paginated:
        if page > 0:
            prev_btn = InlineKeyboardButton(
                text="← prev", callback_data=f"{action}:p:{page-1}",
            )
        else:
            prev_btn = InlineKeyboardButton(text="·", callback_data="noop")
        if page < total_pages - 1:
            next_btn = InlineKeyboardButton(
                text="next →", callback_data=f"{action}:p:{page+1}",
            )
        else:
            next_btn = InlineKeyboardButton(text="·", callback_data="noop")
        mid = InlineKeyboardButton(
            text=f"{page+1}/{total_pages}", callback_data="noop",
        )
        body_rows.append([prev_btn, mid, next_btn])
    if home_to_manage:
        # Picker was launched from /manage. Give the admin a one-tap
        # escape back to the hub instead of stranding them on a chat
        # picker with no back action.
        body_rows.append([_home_button()])
    return InlineKeyboardMarkup(inline_keyboard=body_rows)


def _confirmation_keyboard(original_cb: str) -> InlineKeyboardMarkup:
    """Confirm/cancel keyboard pair for destructive ops.

    Appends ':confirm' and ':cancel' to the supplied callback prefix; the
    handler that owns the prefix must dispatch on the suffix.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="⚠️ YES, do it",
            callback_data=f"{original_cb}:confirm",
        ),
        InlineKeyboardButton(
            text="Cancel",
            callback_data=f"{original_cb}:cancel",
        ),
    ]])


# Cross-link action rows appended to existing admin results. The
# callbacks fall into three top-level prefixes:
#   mst:* — after /memory_stats (or anywhere we render memory diagnostics)
#   mfx:* — after /memory_facts (per-chat) or /memory_summarize_now
#   aip:* — after /ai_provider show
def _home_button() -> InlineKeyboardButton:
    """One-tap escape back to the /manage top menu.

    Appended to every cross-link result keyboard and to chat pickers
    launched from /manage. Keeps the admin from getting stranded N
    layers deep with no way out except retyping the slash command.
    """
    return InlineKeyboardButton(text="🏠 /manage", callback_data="mgm:top")


def _mst_action_row(chat_id: int) -> list[InlineKeyboardButton]:
    """Bottom row after /memory_stats. (One row only; pair with a
    /manage row to build the full keyboard.)"""
    return [
        InlineKeyboardButton(text="Show facts",     callback_data=f"mst:facts:{chat_id}"),
        InlineKeyboardButton(text="Force summarize", callback_data=f"mst:force:{chat_id}"),
        InlineKeyboardButton(text="Edit duckstats", callback_data=f"mst:edit:{chat_id}"),
        InlineKeyboardButton(text="← chats",        callback_data="mst:back"),
    ]


def _mst_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Full keyboard for /memory_stats results: action row + /manage row."""
    return InlineKeyboardMarkup(inline_keyboard=[
        _mst_action_row(chat_id),
        [_home_button()],
    ])


def _mfx_action_row(chat_id: int) -> list[InlineKeyboardButton]:
    """Bottom row after /memory_facts (per-chat result)."""
    return [
        InlineKeyboardButton(text="Force re-extract", callback_data=f"mfx:reextract:{chat_id}"),
        InlineKeyboardButton(text="Stats",            callback_data=f"mfx:stats:{chat_id}"),
        InlineKeyboardButton(text="← chats",          callback_data="mfx:back"),
    ]


def _mfx_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Full keyboard for /memory_facts results: action row + /manage row."""
    return InlineKeyboardMarkup(inline_keyboard=[
        _mfx_action_row(chat_id),
        [_home_button()],
    ])


def _aip_action_row() -> list[InlineKeyboardButton]:
    """Bottom row after /ai_provider show."""
    return [
        InlineKeyboardButton(text="→ Claude",       callback_data="aip:switch:claude"),
        InlineKeyboardButton(text="→ OpenAI",       callback_data="aip:switch:openai"),
        InlineKeyboardButton(text="Claude models",  callback_data="aip:list:claude"),
        InlineKeyboardButton(text="OpenAI models",  callback_data="aip:list:openai"),
    ]


def _aip_keyboard() -> InlineKeyboardMarkup:
    """Full keyboard for /ai_provider results: action row + /manage row."""
    return InlineKeyboardMarkup(inline_keyboard=[
        _aip_action_row(),
        [_home_button()],
    ])


# ----- /manage hub keyboards -------------------------------------------------
# The /manage hub is generated from ipedro.handlers.command_catalog.COMMANDS.
# Top menu = one button per Category in declaration order. Each category
# submenu lists every command tagged that category, generated below.
# Tapping a command-leaf with a wired ``action`` routes to that existing
# mgm:<…> leaf; tapping a card-leaf shows a description + usage hint.
#
# The leaf manifest ``_MGM_LEAVES`` is derived from the catalog so adding a
# new command (or wiring an existing action) updates the manifest
# automatically — no parallel list to keep in sync.

# Category-key → menu callback for the top-level grid. Stable shape so old
# tests that reach for ``mgm:memory`` / ``mgm:duck`` / etc. still resolve;
# the new categories (basics, ai, quotes, …) extend the set.
def _mgm_cat_data(category_key: str) -> str:
    """Callback data for a top-level category button.

    For backwards compatibility, the original five admin categories keep
    their short callback names (mgm:memory, mgm:duck, mgm:ai, mgm:chats,
    mgm:debug). New categories use the generic ``mgm:cat:<key>`` form.
    """
    legacy = {
        "memory":   "mgm:memory",
        "duckhunt": "mgm:duck",
        "ai_admin": "mgm:ai",
        "chats":    "mgm:chats",
        "debug":    "mgm:debug",
    }
    return legacy.get(category_key, f"mgm:cat:{category_key}")


def _mgm_top_keyboard() -> InlineKeyboardMarkup:
    """Top-of-hub keyboard — one row per category, in catalog order."""
    rows = [
        [InlineKeyboardButton(
            text=cat.label, callback_data=_mgm_cat_data(cat.key),
        )]
        for cat in categories_in_order()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _mgm_category_submenu(category_key: str) -> InlineKeyboardMarkup:
    """Submenu for one category: a button per command + a 'back' row.

    Wired commands (action != None) route to their existing mgm: leaf;
    card commands show a usage-hint screen at mgm:cmd:<slug>.
    """
    cat_back = _mgm_cat_data(category_key)
    # The back row links back to the hub. The category-specific submenu
    # IS the second-level screen, so 'back' from here goes to the top.
    rows: list[list[InlineKeyboardButton]] = []
    for cmd in commands_in_category(category_key):
        data = cmd.action or f"mgm:cmd:{cmd.slug}"
        rows.append([InlineKeyboardButton(text=cmd.name, callback_data=data)])
    rows.append([
        InlineKeyboardButton(text="← back", callback_data="mgm:top"),
    ])
    # ``cat_back`` is captured here purely for future use (e.g. nested
    # subsections); intentionally unused at this depth.
    _ = cat_back
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Backwards-compatible aliases for the original five admin submenus. Tests
# (and the legacy dispatcher path) reach for these names — keeping them
# avoids churn.
def _mgm_memory_submenu() -> InlineKeyboardMarkup:
    return _mgm_category_submenu("memory")


def _mgm_duck_submenu() -> InlineKeyboardMarkup:
    """Duckhunt admin submenu (chat-spawn + stats editing).

    The catalog also includes the public /duckhunt /quackflag /duckstats
    rows under the same 'duckhunt' category; admins see all of them here,
    which is the whole point of the unified hub.
    """
    return _mgm_category_submenu("duckhunt")


def _mgm_ai_submenu() -> InlineKeyboardMarkup:
    """AI-provider admin submenu plus cross-links into the aip:* picker."""
    base_rows = list(_mgm_category_submenu("ai_admin").inline_keyboard)
    # Insert provider/model cross-links above the 'back' row so the
    # existing aip:* dispatcher (lives in admin.py's /ai_provider section)
    # remains directly tappable from /manage.
    cross_links = [
        [InlineKeyboardButton(text="→ Claude", callback_data="aip:switch:claude")],
        [InlineKeyboardButton(text="→ OpenAI", callback_data="aip:switch:openai")],
        [InlineKeyboardButton(text="Claude models", callback_data="aip:list:claude")],
        [InlineKeyboardButton(text="OpenAI models", callback_data="aip:list:openai")],
    ]
    base_rows[-1:-1] = cross_links
    return InlineKeyboardMarkup(inline_keyboard=base_rows)


def _mgm_chats_submenu() -> InlineKeyboardMarkup:
    return _mgm_category_submenu("chats")


def _mgm_debug_submenu() -> InlineKeyboardMarkup:
    return _mgm_category_submenu("debug")


def _all_catalog_leaves() -> tuple[str, ...]:
    """Every mgm: callback the catalog can emit."""
    cmd_card_leaves = tuple(f"mgm:cmd:{c.slug}" for c in COMMANDS)
    cat_leaves = tuple(_mgm_cat_data(cat.key) for cat in CATEGORIES)
    return cmd_card_leaves + cat_leaves


# Static action leaves wired to dedicated handlers below the catalog (chat
# pickers, status panels, the silent-chat add flow). Pair with
# _all_catalog_leaves() to get the complete manifest.
_STATIC_MGM_LEAVES: tuple[str, ...] = (
    "mgm:top",
    "mgm:memory:facts", "mgm:memory:stats",
    "mgm:memory:summary", "mgm:memory:force",
    "mgm:duck:edit", "mgm:duck:reset",
    "mgm:duck:spawn", "mgm:duck:spawnall",
    "mgm:ai:show",
    "mgm:chats:list", "mgm:chats:pick", "mgm:chats:config",
    "mgm:chats:silenced", "mgm:chats:silenced:add",
    "mgm:debug:status", "mgm:debug:logs",
    "mgm:debug:cost", "mgm:debug:cmdlog",
    "mgm:debug:toggles", "mgm:debug:cleard",
)


_MGM_LEAVES: tuple[str, ...] = tuple(sorted(set(
    _STATIC_MGM_LEAVES + _all_catalog_leaves()
)))


# Known text models for /ai_model and aip:list pickers. Promotes the
# private price-table keys without coupling admin.py to openai_client
# internals. Update by hand when adding a new model price entry.
_KNOWN_CLAUDE_TEXT_MODELS: tuple[str, ...] = (
    "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
    "claude-sonnet-4-6", "claude-sonnet-4-5", "claude-haiku-4-5",
)
_KNOWN_OPENAI_TEXT_MODELS: tuple[str, ...] = (
    "gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1",
)


# Pending custom-value entries for the /duckstats_edit "Set to custom…"
# button. Keyed by admin user_id → (chat_id, user_id, field, ts).
_PENDING_CUSTOM_VALUES: dict[int, tuple[int, int, str, float]] = {}
_CUSTOM_VALUE_TTL = 60.0  # seconds


def _has_parked_value(msg: "Message") -> bool:
    """Predicate for the custom-value DM handler.

    True only when (a) the sender has a parked entry, (b) the entry is
    fresh (within ``_CUSTOM_VALUE_TTL``), and (c) the message is plain
    text (no leading slash). When the entry has aged past the TTL we
    drop it here so the message keeps propagating to the regular chat
    router instead of being captured by a stale 'custom value' reply.
    """
    if msg.from_user is None or msg.text is None:
        return False
    if msg.text.startswith("/"):
        return False
    uid = msg.from_user.id
    entry = _PENDING_CUSTOM_VALUES.get(uid)
    if entry is None:
        return False
    _, _, _, ts = entry
    if time.time() - ts > _CUSTOM_VALUE_TTL:
        _PENDING_CUSTOM_VALUES.pop(uid, None)
        return False
    return True


def _pop_parked_on_navigation(verb: str, user_id: int | None) -> None:
    """Drop the admin's parked custom-value entry on any dse: navigation
    other than parking itself (``verb == "custom"``).

    Rationale: if the admin previously tapped "Set to custom…" for one
    field and then taps anything else in the dse: flow, they've moved
    on. A later typed number must NOT silently overwrite the originally
    parked field.
    """
    if verb == "custom" or user_id is None:
        return
    _PENDING_CUSTOM_VALUES.pop(user_id, None)


# Whitelist of duck_stats columns the editor is allowed to touch. The
# field-name interpolation in _apply_duckstat_delta / _set_duckstat_field
# is SQL-injection-safe because field is membership-checked here first.
# NEVER add anything outside the six editable fields without a code review.
_DUCKSTAT_EDITABLE_FIELDS: tuple[str, ...] = (
    "points", "killed", "befriended", "misses", "streak", "best_streak",
)


async def _apply_duckstat_delta(
    db, chat_id: int, user_id: int, field: str, delta: int,
) -> tuple[bool, int | None]:
    """Apply a +/-delta to one duckstat field, clamping at 0. Returns
    (ok, new_value). Returns (False, None) when the row doesn't exist
    or the field is not whitelisted.

    The result is clamped to [0, 2_147_483_647] (Postgres INTEGER range)
    so that a large delta on an already-large field can't overflow the
    column and surface as an asyncpg DataError."""
    if field not in _DUCKSTAT_EDITABLE_FIELDS:
        return False, None
    row = await db.fetchrow(
        f"UPDATE duck_stats "
        f"   SET {field} = LEAST(2147483647, GREATEST(0, {field} + $3)) "
        " WHERE chat_id = $1 AND user_id = $2 "
        f"RETURNING {field}",
        chat_id, user_id, delta,
    )
    if row is None:
        return False, None
    return True, int(row[field])


async def _set_duckstat_field(
    db, chat_id: int, user_id: int, field: str, value: int,
) -> tuple[bool, int | None]:
    """Absolute SET (not delta). Clamps to [0, 2_147_483_647] (Postgres
    INTEGER range) so admin-supplied numbers from the 'Set to custom…'
    flow can't overflow the column. Used by 'Set to 0' and custom-value
    entry."""
    if field not in _DUCKSTAT_EDITABLE_FIELDS:
        return False, None
    clamped = max(0, min(2_147_483_647, value))
    row = await db.fetchrow(
        f"UPDATE duck_stats SET {field} = $3 "
        " WHERE chat_id = $1 AND user_id = $2 "
        f"RETURNING {field}",
        chat_id, user_id, clamped,
    )
    if row is None:
        return False, None
    return True, int(row[field])


def _render_duckstat_field_picker(
    chat_id: int, user_id: int, field: str, current_value: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the field-edit keyboard (deltas + set + custom + back)."""
    head = f"{field}: {current_value}\n\nTap a delta or set:"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="-100", callback_data=f"dse:delta:{chat_id}:{user_id}:{field}:-100"),
            InlineKeyboardButton(text="-10",  callback_data=f"dse:delta:{chat_id}:{user_id}:{field}:-10"),
            InlineKeyboardButton(text="-1",   callback_data=f"dse:delta:{chat_id}:{user_id}:{field}:-1"),
            InlineKeyboardButton(text="+1",   callback_data=f"dse:delta:{chat_id}:{user_id}:{field}:+1"),
            InlineKeyboardButton(text="+10",  callback_data=f"dse:delta:{chat_id}:{user_id}:{field}:+10"),
            InlineKeyboardButton(text="+100", callback_data=f"dse:delta:{chat_id}:{user_id}:{field}:+100"),
        ],
        [
            InlineKeyboardButton(text="Set to 0",       callback_data=f"dse:zero:{chat_id}:{user_id}:{field}"),
            InlineKeyboardButton(text="Set to custom…", callback_data=f"dse:custom:{chat_id}:{user_id}:{field}"),
        ],
        [InlineKeyboardButton(text="← back to fields", callback_data=f"dse:user:{chat_id}:{user_id}")],
    ])
    return head, kb


async def _open_picker(
    rt,
    *,
    reply_to: Message | None = None,
    edit_in: Message | None = None,
    prompt: str,
    action: str,
    fetcher=None,
    paginate: bool = True,
    admin_user_id: int | None = None,
    empty_message: str = "No known chats yet.",
    home_to_manage: bool = False,
) -> None:
    """One-stop helper to render a chat picker either as a fresh reply
    (when reply_to is given) or by editing an existing message (edit_in).

    Used by /manage's submenu buttons. Existing slash-command pickers
    don't route through this — they construct their keyboards inline.
    When `home_to_manage` is True, the picker appends a 🏠 /manage row
    so the admin can bail back to the hub instead of being stuck.
    """
    if fetcher is None:
        chats = await rt.chats.list_known()
    else:
        chats = await fetcher()
    kb = _chat_picker(
        chats, action,
        paginate=paginate, page=0,
        admin_user_id=admin_user_id,
        home_to_manage=home_to_manage,
    )
    if kb is None:
        body = empty_message
        # Empty-list case still needs a way back when launched from /manage.
        if home_to_manage:
            kb = InlineKeyboardMarkup(inline_keyboard=[[_home_button()]])
    else:
        body = prompt
    if reply_to:
        await reply_to.reply(
            body, reply_markup=kb, disable_notification=True,
        )
    elif edit_in:
        try:
            await edit_in.edit_text(body, reply_markup=kb)
        except TelegramBadRequest:
            pass


async def _top_users_for_chat(db, chat_id: int, limit: int = 20) -> list[dict]:
    rows = await db.fetch(
        "SELECT user_id, display_name, points, killed, befriended, misses "
        "  FROM duck_stats "
        " WHERE chat_id = $1 "
        " ORDER BY points DESC, killed DESC "
        " LIMIT $2",
        chat_id, limit,
    )
    return [dict(r) for r in rows]


def _user_picker(
    rows: list[dict],
    action: str,
    *,
    with_reset_all_for_chat_id: int | None = None,
) -> InlineKeyboardMarkup | None:
    """Build a per-user picker keyboard. `action` is the callback prefix
    (e.g. 'dsru:CHATID' or 'dse:user:CHATID'). Optionally appends a
    'Reset ALL' bulk button at the bottom when given a chat id."""
    if not rows:
        return None
    kb_rows: list[list[InlineKeyboardButton]] = []
    for r in rows:
        name = (r['display_name'] or str(r['user_id']))[:30]
        label = (
            f"{name} — {r['points']}pts "
            f"({r['killed']}🔫 {r['befriended']}🤝)"
        )[:60]
        kb_rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"{action}:{r['user_id']}",
        )])
    if with_reset_all_for_chat_id is not None:
        kb_rows.append([InlineKeyboardButton(
            text=f"⚠️  Reset ALL {len(rows)} users in this chat",
            callback_data=f"dsra:{with_reset_all_for_chat_id}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


def _render_summary_report(report: dict, chat_id: int) -> str:
    """Format a `force_summarize` return value into the admin-visible
    text body. Used by both the old `on_memory_summarize_now` callback
    and the new mst:/mfx: cross-link buttons."""
    if not report.get("ok"):
        return f"Skipped chat {chat_id}: {report.get('reason', 'unknown')}"
    facts_added = report.get("facts_added", [])
    facts_block = (
        "\n".join(f"  - {f}" for f in facts_added)
        if facts_added else "  (none)"
    )
    body = (
        f"Force-summarized chat {chat_id}.\n"
        f"  messages summarized: {report['messages_summarized']}\n"
        f"  summary id: {report['summary_id']} "
        f"({report['summary_chars']} chars)\n"
        f"  facts added ({len(facts_added)}):\n{facts_block}"
    )
    # When 0 facts were extracted, show what the LLM actually said so the
    # admin can tell "LLM is being too conservative" apart from "LLM never
    # ran" / "DB write failed".
    raw = report.get("raw_facts_response")
    if not facts_added and raw is not None:
        body += f"\n\n  raw LLM facts response: {raw!r}"
    return body


async def _with_working_placeholder(
    cb,
    head_text: str,
    work,
) -> None:
    """Edit cb.message to '<head>\n\n⏳ Working…' immediately, run work(),
    then re-edit to its result. On exception, edit to '❌ Failed: <reason>'.

    `work` is a zero-arg callable returning an awaitable that resolves to
    (body, kb) where body is str and kb is InlineKeyboardMarkup | None.
    """
    if cb.message:
        try:
            await cb.message.edit_text(f"{head_text}\n\n⏳ Working…")
        except TelegramBadRequest:
            pass
    try:
        body, kb = await work()
    except Exception as exc:
        log.warning("callback %s failed: %s", cb.data, exc)
        if cb.message:
            try:
                await cb.message.edit_text(f"❌ Failed: {exc}")
            except TelegramBadRequest:
                pass
        return
    if cb.message:
        try:
            await cb.message.edit_text(body[:4000], reply_markup=kb)
        except TelegramBadRequest:
            pass


def build_router(rt: Runtime) -> Router:
    r = Router(name="admin")
    admin_ids = rt.settings.admin_ids

    async def _gate_callback(cb: CallbackQuery) -> bool:
        if not cb.from_user or not is_admin_user(cb.from_user.id, admin_ids):
            await cb.answer("Admin only.", show_alert=True)
            return False
        return True

    @r.message(Command("list_chat_ids"))
    async def list_chat_ids(msg: Message) -> None:
        if not await require_admin(msg, admin_ids):
            return
        rows = await rt.chats.list_known()
        if not rows:
            await msg.reply("No known chats yet.")
            return
        lines = [
            f"{r['chat_id']:>15} {r['type']:<10} {r['title'] or ''}"
            for r in rows
        ]
        text = "Known chats (last seen first):\n" + "\n".join(lines)
        # Telegram limits message length to 4096 chars.
        await msg.reply(text[:4000], disable_notification=True)

    @r.message(Command("send_message"))
    async def send_message_cmd(msg: Message) -> None:
        if not await require_admin(msg, admin_ids):
            return
        args = (msg.text or "").split(" ", 2)
        if len(args) < 3:
            await msg.reply(
                "Usage: /send_message <chat_id> <text>",
                disable_notification=True,
            )
            return
        try:
            target = int(args[1])
        except ValueError:
            await msg.reply("Invalid chat id.", disable_notification=True)
            return
        body = args[2]
        try:
            await rt.bot.send_message(target, body, disable_notification=True)
            await msg.reply("Sent.", disable_notification=True)
        except TelegramBadRequest as exc:
            await msg.reply(f"Send failed: {exc.message}", disable_notification=True)

    @r.message(Command("logs"))
    async def logs_cmd(msg: Message) -> None:
        """Tail the bot's actual program logs.

        Usage: /logs [N] [filter]   e.g. /logs 50 spawner
        N defaults to 50, filter is a case-insensitive substring.
        """
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split(None, 2)
        limit = 50
        needle: str | None = None
        if len(parts) >= 2:
            try:
                limit = max(1, min(200, int(parts[1])))
            except ValueError:
                needle = parts[1]
        if len(parts) >= 3:
            needle = parts[2]
        lines = recent_log_lines(limit=limit, contains=needle)
        if not lines:
            await msg.reply(
                "No log lines (yet, or none match the filter).",
                disable_notification=True,
            )
            return
        body = "\n".join(lines)
        # Telegram caps a single message at 4096 chars; chunk and send.
        chunks: list[str] = []
        buf = ""
        for ln in lines:
            extra = ln + "\n"
            if len(buf) + len(extra) > 3800:
                chunks.append(buf)
                buf = extra
            else:
                buf += extra
        if buf:
            chunks.append(buf)
        for i, chunk in enumerate(chunks[:5], 1):
            header = f"-- logs ({i}/{len(chunks)}) --\n" if len(chunks) > 1 else ""
            await msg.reply(header + chunk, disable_notification=True)

    @r.message(Command("cmdlog"))
    async def cmdlog(msg: Message) -> None:
        """Command audit log (was previously /logs)."""
        if not await require_admin(msg, admin_ids):
            return
        rows = await rt.command_log.tail(30)
        if not rows:
            await msg.reply("No command log entries yet.")
            return
        lines = [
            f"{r['created_at']:%H:%M:%S} chat={r['chat_id']} user={r['user_id']}"
            f" {r['command']} ok={r['success']}"
            + (f" err={r['error']}" if r["error"] else "")
            for r in rows
        ]
        await msg.reply("Recent commands:\n" + "\n".join(lines), disable_notification=True)

    @r.message(Command("quack_all"))
    async def quack_all(msg: Message) -> None:
        """Spawn a duck in every duckhunt-enabled chat that doesn't already have one."""
        if not await require_admin(msg, admin_ids):
            return
        chat_ids = await duckhunt_enabled_chat_ids(rt.db)
        if not chat_ids:
            await msg.reply(
                "No chats have duckhunt enabled.", disable_notification=True,
            )
            return
        spawned, skipped, failed = 0, 0, 0
        for chat_id in chat_ids:
            if await rt.duckhunt.active_duck(chat_id):
                skipped += 1
                continue
            try:
                duck = await rt.duckhunt.spawn_duck(
                    chat_id, rt.settings.duckhunt_duck_lifetime_seconds,
                )
                text = await build_quack_message(rt.openai, duck.rarity)
                await rt.bot.send_message(
                    chat_id, text, disable_notification=True,
                )
                spawned += 1
                log.info(
                    "quack_all: spawned in chat=%s event_id=%s",
                    chat_id, duck.id,
                )
            except Exception as exc:
                failed += 1
                log.warning("quack_all failed for chat %s: %s", chat_id, exc)
        await msg.reply(
            f"Quacked in {spawned} chat(s). "
            f"Skipped {skipped} (duck already active). "
            f"Failed {failed}.",
            disable_notification=True,
        )

    @r.message(Command("quack_chat"))
    async def quack_chat(msg: Message) -> None:
        """Pick a duckhunt-enabled chat from a keyboard and spawn a duck there."""
        if not await require_admin(msg, admin_ids):
            return
        chats = await duckhunt_enabled_chats(rt.db)
        kb = _chat_picker(
            chats, "qchat",
            paginate=True,
            admin_user_id=msg.from_user.id if msg.from_user else None,
        )
        if kb is None:
            await msg.reply(
                "No chats have duckhunt enabled.", disable_notification=True,
            )
            return
        await msg.reply(
            "Pick a chat to spawn a duck in:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("qchat:"))
    async def on_quack_chat(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parsed = _parse_picker_cb(cb.data)
        if parsed is None:
            await cb.answer(_expired("quack_chat"), show_alert=True)
            return
        kind, n = parsed
        if kind == "page":
            chats = await duckhunt_enabled_chats(rt.db)
            kb = _chat_picker(
                chats, "qchat", paginate=True, page=n,
                admin_user_id=cb.from_user.id if cb.from_user else None,
            )
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        target = n
        if cb.from_user is not None:
            _LAST_PICKED_CHAT[cb.from_user.id] = (target, time.time())
        if await rt.duckhunt.active_duck(target):
            await cb.answer("That chat already has an active duck.", show_alert=True)
            return
        try:
            duck = await rt.duckhunt.spawn_duck(
                target, rt.settings.duckhunt_duck_lifetime_seconds,
            )
            text = await build_quack_message(rt.openai, duck.rarity)
            await rt.bot.send_message(target, text, disable_notification=True)
        except Exception as exc:
            log.warning("quack_chat failed for chat %s: %s", target, exc)
            await cb.answer(f"Failed: {exc}", show_alert=True)
            return
        log.info(
            "quack_chat: spawned in chat=%s event_id=%s",
            target, duck.id,
        )
        if cb.message:
            try:
                await cb.message.edit_text(
                    f"🦆 Spawned in {target} (duck id: {duck.id}).",
                )
            except TelegramBadRequest:
                pass
        await cb.answer("Quacked.")

    @r.message(Command("facts_chat"))
    async def facts_chat(msg: Message) -> None:
        """Pick a chat from a keyboard and dump its durable facts."""
        if not await require_admin(msg, admin_ids):
            return
        chats = await rt.chats.list_known()
        kb = _chat_picker(
            chats, "fchat",
            paginate=True,
            admin_user_id=msg.from_user.id if msg.from_user else None,
        )
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat to show stored facts for:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("fchat:"))
    async def on_facts_chat(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parsed = _parse_picker_cb(cb.data)
        if parsed is None:
            await cb.answer(_expired("facts_chat"), show_alert=True)
            return
        kind, n = parsed
        if kind == "page":
            chats = await rt.chats.list_known()
            kb = _chat_picker(
                chats, "fchat", paginate=True, page=n,
                admin_user_id=cb.from_user.id if cb.from_user else None,
            )
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        target = n
        if cb.from_user is not None:
            _LAST_PICKED_CHAT[cb.from_user.id] = (target, time.time())
        await _send_facts_for(target, reply_to=None, edit_in=cb.message)
        await cb.answer()

    @r.message(Command("pick_chat"))
    async def pick_chat(msg: Message) -> None:
        """Pick a chat from a keyboard; the bot replies with its id for copy-paste."""
        if not await require_admin(msg, admin_ids):
            return
        chats = await rt.chats.list_known()
        kb = _chat_picker(
            chats, "pchat",
            paginate=True,
            admin_user_id=msg.from_user.id if msg.from_user else None,
        )
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat to copy its id:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("pchat:"))
    async def on_pick_chat(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parsed = _parse_picker_cb(cb.data)
        if parsed is None:
            await cb.answer(_expired("pick_chat"), show_alert=True)
            return
        kind, n = parsed
        if kind == "page":
            chats = await rt.chats.list_known()
            kb = _chat_picker(
                chats, "pchat", paginate=True, page=n,
                admin_user_id=cb.from_user.id if cb.from_user else None,
            )
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        target = n
        if cb.from_user is not None:
            _LAST_PICKED_CHAT[cb.from_user.id] = (target, time.time())
        if cb.message:
            try:
                await cb.message.edit_text(
                    f"Chat id: <code>{target}</code>\n"
                    f"Use with: /send_message {target} your text",
                    parse_mode="HTML",
                )
            except TelegramBadRequest:
                pass
        await cb.answer(str(target))

    async def _render_config_for(target_chat_id: int, *, reply_to: Message | None,
                                 edit_in: Message | None) -> None:
        """Render the inline-keyboard config wizard for a target chat, scoped to
        an admin DM. Re-uses utility.py's wizard primitives so we have exactly
        one wizard code-path."""
        # Local import — utility imports nothing from admin, but the inverse
        # would require pulling utility's whole router graph if done at module
        # top. Keeping the import inside the function keeps the cycle clean.
        from ipedro.handlers.utility import (
            _config_keyboard, _config_wizard_header,
        )
        # Ensure the row exists so the wizard has something to toggle.
        cfg = await rt.chats.get_config(target_chat_id)
        if cfg is None:
            # No /config has ever run in the target chat. Seed with defaults
            # so the wizard isn't an empty shell on first open. We don't have
            # a Message scoped to that chat here, so use settings directly.
            s = rt.settings
            cfg = await rt.chats.upsert_default_config(
                target_chat_id,
                response_policy=s.default_response_policy_group,
                ambient_probability=s.default_ambient_probability,
                persona=s.default_persona,
                duckhunt_enabled=s.duckhunt_enabled_by_default,
            )
        body = _config_wizard_header(cfg, target_chat_id, is_dm_scoped=True)
        kb = _config_keyboard(cfg, target_chat_id=target_chat_id)
        if reply_to:
            await reply_to.reply(
                body, reply_markup=kb, disable_notification=True,
                parse_mode="HTML",
            )
        elif edit_in:
            try:
                await edit_in.edit_text(body, reply_markup=kb, parse_mode="HTML")
            except TelegramBadRequest:
                pass

    @r.message(Command("config_for"))
    async def config_for(msg: Message) -> None:
        """Open the /config wizard from a DM, scoped to any known chat.

        /config_for             → chat picker, then wizard
        /config_for <chat_id>   → wizard directly for that chat
        """
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split()
        if len(parts) >= 2:
            try:
                target = int(parts[1])
            except ValueError:
                await msg.reply("Invalid chat id.", disable_notification=True)
                return
            await _render_config_for(target, reply_to=msg, edit_in=None)
            return
        chats = await rt.chats.list_known()
        kb = _chat_picker(
            chats, "cfgfor",
            paginate=True,
            admin_user_id=msg.from_user.id if msg.from_user else None,
            home_to_manage=False,
        )
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat to configure:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("cfgfor:"))
    async def on_config_for(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parsed = _parse_picker_cb(cb.data)
        if parsed is None:
            await cb.answer(_expired("config_for"), show_alert=True)
            return
        kind, n = parsed
        if kind == "page":
            chats = await rt.chats.list_known()
            kb = _chat_picker(
                chats, "cfgfor", paginate=True, page=n,
                admin_user_id=cb.from_user.id if cb.from_user else None,
                home_to_manage=False,
            )
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        target = n
        if cb.from_user is not None:
            _LAST_PICKED_CHAT[cb.from_user.id] = (target, time.time())
        await _render_config_for(target, reply_to=None, edit_in=cb.message)
        await cb.answer()

    # -------------------------------- /delete_msg + /delete_last ------------
    def _format_tracked_entry(entry: TrackedMessage) -> str:
        age = max(0, int(time.time() - entry.sent_at))
        if age < 60:
            ago = f"{age}s ago"
        elif age < 3600:
            ago = f"{age // 60}m ago"
        elif age < 86400:
            ago = f"{age // 3600}h ago"
        else:
            ago = f"{age // 86400}d ago"
        snippet = entry.snippet or "(no preview)"
        return f"{ago} #{entry.message_id} — {snippet}"[:60]

    def _delete_msg_picker(chat_id: int) -> InlineKeyboardMarkup | None:
        """Render the per-message list for /delete_msg."""
        entries = recent_tracked(chat_id)
        if not entries:
            return None
        rows: list[list[InlineKeyboardButton]] = []
        for e in entries:
            rows.append([InlineKeyboardButton(
                text=_format_tracked_entry(e),
                callback_data=f"dmsg:{chat_id}:{e.message_id}",
            )])
        rows.append([InlineKeyboardButton(
            text="← chats", callback_data="dmsg:back",
        )])
        rows.append([_home_button()])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def _do_delete_bot_message(target_chat_id: int, message_id: int) -> bool:
        """Attempt the actual Telegram delete. Returns True iff it succeeded."""
        try:
            await rt.bot.delete_message(target_chat_id, message_id)
            forget_tracked(target_chat_id, message_id)
            return True
        except TelegramBadRequest as exc:
            log.warning(
                "delete_message failed chat=%s msg=%s: %s",
                target_chat_id, message_id, exc,
            )
            return False
        except Exception as exc:  # pragma: no cover
            log.warning(
                "delete_message error chat=%s msg=%s: %s",
                target_chat_id, message_id, exc,
            )
            return False

    @r.message(Command("delete_msg"))
    async def delete_msg(msg: Message) -> None:
        """Delete one of the bot's recent messages in a chat. Admin DM only.

        /delete_msg                → chat picker → message picker
        /delete_msg <chat_id>      → message picker for that chat
        """
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split()
        if len(parts) >= 2:
            try:
                target = int(parts[1])
            except ValueError:
                await msg.reply("Invalid chat id.", disable_notification=True)
                return
            kb = _delete_msg_picker(target)
            if kb is None:
                await msg.reply(
                    f"No recent bot messages tracked for chat {target} "
                    "(buffer is in-memory; restarts wipe it).",
                    disable_notification=True,
                )
                return
            await msg.reply(
                f"Pick a bot message in chat {target} to delete:",
                reply_markup=kb,
                disable_notification=True,
            )
            return
        chats = await rt.chats.list_known()
        kb = _chat_picker(
            chats, "dmsgchat",
            paginate=True,
            admin_user_id=msg.from_user.id if msg.from_user else None,
            home_to_manage=False,
        )
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat to delete a bot message in:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("dmsgchat:"))
    async def on_delete_msg_chat(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parsed = _parse_picker_cb(cb.data)
        if parsed is None:
            await cb.answer(_expired("delete_msg"), show_alert=True)
            return
        kind, n = parsed
        if kind == "page":
            chats = await rt.chats.list_known()
            kb = _chat_picker(
                chats, "dmsgchat", paginate=True, page=n,
                admin_user_id=cb.from_user.id if cb.from_user else None,
                home_to_manage=False,
            )
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        target = n
        if cb.from_user is not None:
            _LAST_PICKED_CHAT[cb.from_user.id] = (target, time.time())
        kb = _delete_msg_picker(target)
        if cb.message:
            if kb is None:
                try:
                    await cb.message.edit_text(
                        f"No recent bot messages tracked for chat {target}.",
                    )
                except TelegramBadRequest:
                    pass
            else:
                try:
                    await cb.message.edit_text(
                        f"Pick a bot message in chat {target} to delete:",
                        reply_markup=kb,
                    )
                except TelegramBadRequest:
                    pass
        await cb.answer()

    @r.callback_query(F.data.startswith("dmsg:"))
    async def on_delete_msg(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parts = cb.data.split(":")
        # dmsg:back → re-open the chat picker
        if len(parts) >= 2 and parts[1] == "back":
            await _open_picker(
                rt, edit_in=cb.message,
                prompt="Pick a chat to delete a bot message in:",
                action="dmsgchat",
                admin_user_id=cb.from_user.id if cb.from_user else None,
                home_to_manage=False,
            )
            await cb.answer()
            return
        if len(parts) < 3:
            await cb.answer(_expired("delete_msg"), show_alert=True)
            return
        try:
            target = int(parts[1])
            message_id = int(parts[2])
        except ValueError:
            await cb.answer(_expired("delete_msg"), show_alert=True)
            return
        ok = await _do_delete_bot_message(target, message_id)
        # Re-render the picker so the deleted row is gone.
        if cb.message:
            kb = _delete_msg_picker(target)
            if kb is None:
                try:
                    await cb.message.edit_text(
                        f"Deleted. No more tracked messages in chat {target}."
                        if ok else
                        f"Couldn't delete msg #{message_id} in chat {target} "
                        "(too old? bot lacks delete perms?).",
                    )
                except TelegramBadRequest:
                    pass
            else:
                head = (
                    f"Deleted #{message_id}. Pick another:"
                    if ok else
                    f"Couldn't delete #{message_id}. Pick another:"
                )
                try:
                    await cb.message.edit_text(head, reply_markup=kb)
                except TelegramBadRequest:
                    pass
        await cb.answer("Deleted." if ok else "Delete failed.")

    @r.message(Command("delete_last"))
    async def delete_last(msg: Message) -> None:
        """Delete the bot's last N messages in a picked chat. Admin DM only.

        /delete_last                       → picker; defaults N=1
        /delete_last <N>                   → picker; defaults to N (≤ 20)
        /delete_last <chat_id> <N>         → direct
        """
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split()
        n: int = 1
        chat_id: int | None = None
        if len(parts) == 2:
            try:
                n = max(1, min(20, int(parts[1])))
            except ValueError:
                await msg.reply("Bad N.", disable_notification=True)
                return
        elif len(parts) >= 3:
            try:
                chat_id = int(parts[1])
                n = max(1, min(20, int(parts[2])))
            except ValueError:
                await msg.reply(
                    "Usage: /delete_last [chat_id] <N>",
                    disable_notification=True,
                )
                return
        if chat_id is not None:
            if n > 1:
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=f"⚠️ Delete last {n}",
                        callback_data=f"dlast:{chat_id}:{n}:confirm",
                    ),
                    InlineKeyboardButton(
                        text="Cancel",
                        callback_data=f"dlast:{chat_id}:{n}:cancel",
                    ),
                ]])
                await msg.reply(
                    f"Delete the last {n} bot messages in chat {chat_id}?",
                    reply_markup=kb, disable_notification=True,
                )
                return
            # N == 1, just do it.
            await _execute_delete_last(msg, chat_id, 1)
            return
        # No chat id → picker. Stash the N on the callback prefix.
        chats = await rt.chats.list_known()
        kb = _chat_picker(
            chats, f"dlastch:{n}",
            paginate=True,
            admin_user_id=msg.from_user.id if msg.from_user else None,
            home_to_manage=False,
        )
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            f"Pick a chat to delete the last {n} bot message(s) in:",
            reply_markup=kb, disable_notification=True,
        )

    async def _execute_delete_last(
        target_msg: Message | None,
        chat_id: int,
        n: int,
        edit_in: Message | None = None,
    ) -> None:
        entries = recent_tracked(chat_id)[:n]
        if not entries:
            body = f"No recent bot messages tracked for chat {chat_id}."
        else:
            ok = 0
            for e in entries:
                if await _do_delete_bot_message(chat_id, e.message_id):
                    ok += 1
            body = (
                f"Deleted {ok}/{len(entries)} message(s) in chat {chat_id}."
            )
        if target_msg is not None:
            await target_msg.reply(body, disable_notification=True)
        elif edit_in is not None:
            try:
                await edit_in.edit_text(body)
            except TelegramBadRequest:
                pass

    @r.callback_query(F.data.startswith("dlastch:"))
    async def on_delete_last_chat(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        # dlastch:N:CHATID  or  dlastch:N:p:PAGE
        parts = cb.data.split(":")
        if len(parts) < 3:
            await cb.answer(_expired("delete_last"), show_alert=True)
            return
        try:
            n = max(1, min(20, int(parts[1])))
        except ValueError:
            await cb.answer(_expired("delete_last"), show_alert=True)
            return
        # Pagination?
        if len(parts) >= 4 and parts[2] == "p":
            try:
                page = int(parts[3])
            except ValueError:
                await cb.answer(_expired("delete_last"), show_alert=True)
                return
            chats = await rt.chats.list_known()
            kb = _chat_picker(
                chats, f"dlastch:{n}", paginate=True, page=page,
                admin_user_id=cb.from_user.id if cb.from_user else None,
                home_to_manage=False,
            )
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        try:
            target = int(parts[2])
        except ValueError:
            await cb.answer(_expired("delete_last"), show_alert=True)
            return
        if cb.from_user is not None:
            _LAST_PICKED_CHAT[cb.from_user.id] = (target, time.time())
        if n > 1 and cb.message:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=f"⚠️ Delete last {n}",
                    callback_data=f"dlast:{target}:{n}:confirm",
                ),
                InlineKeyboardButton(
                    text="Cancel",
                    callback_data=f"dlast:{target}:{n}:cancel",
                ),
            ]])
            try:
                await cb.message.edit_text(
                    f"Delete the last {n} bot messages in chat {target}?",
                    reply_markup=kb,
                )
            except TelegramBadRequest:
                pass
            await cb.answer()
            return
        # N == 1, just do it.
        await _execute_delete_last(None, target, 1, edit_in=cb.message)
        await cb.answer("Deleted.")

    @r.callback_query(F.data.startswith("dlast:"))
    async def on_delete_last_confirm(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parts = cb.data.split(":")
        if len(parts) < 4:
            await cb.answer(_expired("delete_last"), show_alert=True)
            return
        try:
            target = int(parts[1])
            n = max(1, min(20, int(parts[2])))
        except ValueError:
            await cb.answer(_expired("delete_last"), show_alert=True)
            return
        suffix = parts[3]
        if suffix == "cancel":
            if cb.message:
                try:
                    await cb.message.edit_text("Cancelled.")
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        await _execute_delete_last(None, target, n, edit_in=cb.message)
        await cb.answer("Deleted.")

    # -------------------------------- silenced-chats (admin-only override) ---
    @r.message(Command("silenced_chats"))
    async def silenced_chats_cmd(msg: Message) -> None:
        """List the chats currently silenced for the ambient loops."""
        if not await require_admin(msg, admin_ids):
            return
        ids = list_silenced()
        if not ids:
            await msg.reply(
                "No chats silenced. Use /silent_chat <chat_id> to add one.",
                disable_notification=True,
            )
            return
        chats_map = {c["chat_id"]: c for c in await rt.chats.list_known()}
        lines = ["🤫 Silenced chats:"]
        for cid in ids:
            ch = chats_map.get(cid)
            title = (ch.get("title") if ch else None) or "(unknown)"
            lines.append(f"  {cid} — {title}")
        lines.append("\nUnsilence with /unsilent_chat <chat_id>.")
        await msg.reply("\n".join(lines)[:4000], disable_notification=True)

    @r.message(Command("silent_chat"))
    async def silent_chat_cmd(msg: Message) -> None:
        """Silence a chat — celebrations / fortune / retro / confession sends
        will be posted with disable_notification=True. Admin DM only."""
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split()
        if len(parts) < 2:
            await msg.reply(
                "Usage: /silent_chat <chat_id>", disable_notification=True,
            )
            return
        try:
            chat_id = int(parts[1])
        except ValueError:
            await msg.reply("Invalid chat id.", disable_notification=True)
            return
        added = await silence_chat(rt.db, chat_id)
        await msg.reply(
            f"Silenced chat {chat_id}." if added else
            f"Chat {chat_id} was already silenced.",
            disable_notification=True,
        )

    @r.message(Command("unsilent_chat"))
    async def unsilent_chat_cmd(msg: Message) -> None:
        """Drop a chat from the silenced set. Admin DM only."""
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split()
        if len(parts) < 2:
            await msg.reply(
                "Usage: /unsilent_chat <chat_id>", disable_notification=True,
            )
            return
        try:
            chat_id = int(parts[1])
        except ValueError:
            await msg.reply("Invalid chat id.", disable_notification=True)
            return
        removed = await unsilence_chat(rt.db, chat_id)
        await msg.reply(
            f"Unsilenced chat {chat_id}." if removed else
            f"Chat {chat_id} was not silenced.",
            disable_notification=True,
        )

    async def _render_silenced_panel(edit_in: Message) -> None:
        ids = list_silenced()
        chats_map = {c["chat_id"]: c for c in await rt.chats.list_known()}
        lines = [
            "🤫 Silenced chats — bot suppresses notifications on these chats "
            "for ambient sends (celebrations / fortune / retro / confession).",
            "",
        ]
        rows: list[list[InlineKeyboardButton]] = []
        if not ids:
            lines.append("(none silenced)")
        else:
            for cid in ids:
                ch = chats_map.get(cid)
                title = (ch.get("title") if ch else None) or "(unknown)"
                lines.append(f"  {cid} — {title}")
                rows.append([InlineKeyboardButton(
                    text=f"🔔 Unsilence {title[:30]}",
                    callback_data=f"unsil:{cid}",
                )])
        rows.append([InlineKeyboardButton(
            text="🤫 Silence a chat…", callback_data="mgm:chats:silenced:add",
        )])
        rows.append([
            InlineKeyboardButton(text="← back", callback_data="mgm:chats"),
            _home_button(),
        ])
        try:
            await edit_in.edit_text(
                "\n".join(lines)[:4000],
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        except TelegramBadRequest:
            pass

    @r.callback_query(F.data.startswith("unsil:"))
    async def on_unsilence(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parts = cb.data.split(":")
        if len(parts) < 2:
            await cb.answer(_expired("unsilence"), show_alert=True)
            return
        try:
            chat_id = int(parts[1])
        except ValueError:
            await cb.answer(_expired("unsilence"), show_alert=True)
            return
        await unsilence_chat(rt.db, chat_id)
        if cb.message:
            await _render_silenced_panel(cb.message)
        await cb.answer(f"Unsilenced {chat_id}.")

    @r.callback_query(F.data.startswith("silch:"))
    async def on_silence_picker(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parts = cb.data.split(":")
        if len(parts) < 2:
            await cb.answer(_expired("silence"), show_alert=True)
            return
        # Pagination: silch:p:N
        if parts[1] == "p" and len(parts) >= 3:
            try:
                page = int(parts[2])
            except ValueError:
                await cb.answer(_expired("silence"), show_alert=True)
                return
            chats = await rt.chats.list_known()
            kb = _chat_picker(
                chats, "silch", paginate=True, page=page,
                admin_user_id=cb.from_user.id if cb.from_user else None,
                home_to_manage=False,
            )
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        try:
            chat_id = int(parts[1])
        except ValueError:
            await cb.answer(_expired("silence"), show_alert=True)
            return
        if is_silenced(chat_id):
            await cb.answer(f"Chat {chat_id} already silenced.")
        else:
            await silence_chat(rt.db, chat_id)
            await cb.answer(f"Silenced {chat_id}.")
        if cb.message:
            await _render_silenced_panel(cb.message)

    @r.message(Command("ai_provider"))
    async def ai_provider(msg: Message) -> None:
        """Switch the text-completion provider. /ai_provider show|claude|openai."""
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split()
        if len(parts) < 2 or parts[1].lower() == "show":
            await msg.reply(
                f"Current text provider: {rt.openai.text_provider}\n"
                f"  claude model: {rt.openai.claude_model}\n"
                f"  openai model: {rt.openai.text_model}\n"
                "Switch with: /ai_provider claude  or  /ai_provider openai",
                reply_markup=_aip_keyboard(),
                disable_notification=True,
            )
            return
        choice = parts[1].lower()
        if choice not in ("claude", "openai"):
            await msg.reply(
                "Usage: /ai_provider show | claude | openai",
                disable_notification=True,
            )
            return
        try:
            rt.openai.set_text_provider(choice)
        except ValueError as exc:
            await msg.reply(f"Can't switch: {exc}", disable_notification=True)
            return
        await kv_set(rt.db, "text_provider", choice)
        await msg.reply(
            f"Text provider is now {choice}.",
            disable_notification=True,
        )

    @r.message(Command("ai_model"))
    async def ai_model(msg: Message) -> None:
        """Switch the text model used by the active provider.

        /ai_model show
        /ai_model <model_id>          # picks the right slot for the active provider
        /ai_model claude <model_id>   # explicitly set the Claude model
        /ai_model openai <model_id>   # explicitly set the OpenAI model
        """
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split()
        if len(parts) < 2 or parts[1].lower() == "show":
            await msg.reply(
                f"Provider: {rt.openai.text_provider}\n"
                f"  claude model: {rt.openai.claude_model}\n"
                f"  openai model: {rt.openai.text_model}",
                disable_notification=True,
            )
            return
        if len(parts) >= 3 and parts[1].lower() in ("claude", "openai"):
            slot = parts[1].lower()
            new_model = parts[2]
        else:
            slot = rt.openai.text_provider
            new_model = parts[1]
        if slot == "claude":
            rt.openai.set_claude_model(new_model)
            await kv_set(rt.db, "claude_text_model", new_model)
        else:
            rt.openai.set_openai_text_model(new_model)
            await kv_set(rt.db, "openai_text_model", new_model)
        await msg.reply(
            f"{slot} text model is now {new_model}.",
            disable_notification=True,
        )

    async def _store_master_prompt(msg: Message, new_text: str) -> None:
        """Persist a new master prompt and surface a token-budget warning
        if it's large enough that build_context() would refuse to include it."""
        new_text = new_text.strip()
        await kv_set(rt.db, "master_prompt", new_text)
        set_master_prompt_override(new_text)
        tokens = count_tokens(new_text)
        budget = rt.settings.context_max_tokens
        suffix = ""
        if tokens >= budget:
            suffix = (
                f"\n\n⚠️ {tokens} tokens exceeds context_max_tokens ({budget}); "
                "the persona will be dropped at runtime. Shorten the prompt "
                "or raise CONTEXT_MAX_TOKENS."
            )
        elif tokens > budget * 0.75:
            suffix = (
                f"\n\n⚠️ {tokens} tokens uses >75% of the {budget}-token "
                "context budget; little room left for memory/history."
            )
        await msg.reply(
            f"Master prompt updated ({len(new_text)} chars, "
            f"~{tokens} tokens).{suffix}",
            disable_notification=True,
        )

    @r.message(Command("master_prompt"))
    async def master_prompt(msg: Message) -> None:
        """View / set / reset the global master persona prompt."""
        if not await require_admin(msg, admin_ids):
            return
        # Command may arrive as msg.text or as msg.caption (when a document
        # is uploaded with the command as its caption).
        raw = (msg.text or msg.caption or "").split(None, 2)
        sub = raw[1].lower() if len(raw) >= 2 else "show"
        if sub == "show":
            current = current_master_prompt()
            is_default = current == DEFAULT_DUDE_PROMPT
            tag = "(default Dale)" if is_default else "(override active)"
            tokens = count_tokens(current)
            head = (
                f"Master persona prompt {tag} "
                f"({len(current)} chars, ~{tokens} tokens):\n\n"
            )
            # Keep the whole reply under Telegram's 4096-char outbound limit.
            await msg.reply(
                head + current[: 4000 - len(head)],
                disable_notification=True,
            )
            return
        if sub == "reset":
            await kv_delete(rt.db, "master_prompt")
            await kv_delete(rt.db, "pedro_master_prompt")  # legacy key
            set_master_prompt_override(None)
            await msg.reply(
                "Reset to default Dale prompt.", disable_notification=True,
            )
            return
        if sub == "set":
            if len(raw) < 3 or not raw[2].strip():
                await msg.reply(
                    "Usage: /master_prompt set <new full prompt text>\n"
                    "Or attach a .txt file and reply to it with "
                    "/master_prompt setfile (for prompts longer than 4079 "
                    "chars).",
                    disable_notification=True,
                )
                return
            await _store_master_prompt(msg, raw[2])
            return
        if sub == "setfile":
            # Accept the document either attached to this message (as caption)
            # or in a message we're replying to.
            doc = msg.document or (
                msg.reply_to_message.document if msg.reply_to_message else None
            )
            if doc is None:
                await msg.reply(
                    "Send the new prompt as a .txt file and either caption "
                    "it `/master_prompt setfile` or reply to the file with "
                    "`/master_prompt setfile`.",
                    disable_notification=True,
                )
                return
            if doc.file_size and doc.file_size > _MASTER_PROMPT_FILE_MAX_BYTES:
                await msg.reply(
                    f"That file is {doc.file_size} bytes; cap is "
                    f"{_MASTER_PROMPT_FILE_MAX_BYTES}. Shorten the prompt.",
                    disable_notification=True,
                )
                return
            try:
                file = await msg.bot.get_file(doc.file_id)
                buf = io.BytesIO()
                await msg.bot.download_file(file.file_path, destination=buf)
            except Exception as exc:
                log.warning("master_prompt setfile download failed: %s", exc)
                await msg.reply(
                    f"Couldn't download that file: {exc}",
                    disable_notification=True,
                )
                return
            data = buf.getvalue()
            if len(data) > _MASTER_PROMPT_FILE_MAX_BYTES:
                await msg.reply(
                    f"File is {len(data)} bytes; cap is "
                    f"{_MASTER_PROMPT_FILE_MAX_BYTES}.",
                    disable_notification=True,
                )
                return
            try:
                new_text = data.decode("utf-8")
            except UnicodeDecodeError:
                await msg.reply(
                    "File isn't valid UTF-8. Save it as plain UTF-8 text "
                    "and try again.",
                    disable_notification=True,
                )
                return
            if not new_text.strip():
                await msg.reply(
                    "File is empty — refusing to clobber the prompt.",
                    disable_notification=True,
                )
                return
            await _store_master_prompt(msg, new_text)
            return
        await msg.reply(
            "Usage: /master_prompt show | set <text> | setfile | reset",
            disable_notification=True,
        )

    @r.message(Command("cost"))
    async def cost(msg: Message) -> None:
        """Show OpenAI spend (last 7 days). /cost or /cost <chat_id>."""
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split()
        chat_filter = None
        if len(parts) >= 2:
            try:
                chat_filter = int(parts[1])
            except ValueError:
                await msg.reply("Bad chat id.", disable_notification=True)
                return
        if chat_filter is not None:
            rows = await rt.db.fetch(
                "SELECT kind, COUNT(*) AS calls, "
                "       COALESCE(SUM(total_tokens), 0) AS tokens, "
                "       COALESCE(SUM(cost_usd), 0) AS cost "
                "  FROM openai_usage "
                " WHERE chat_id = $1 AND created_at >= NOW() - INTERVAL '7 days' "
                " GROUP BY kind ORDER BY cost DESC",
                chat_filter,
            )
            header = f"Last 7d for chat {chat_filter}:"
        else:
            rows = await rt.db.fetch(
                "SELECT kind, COUNT(*) AS calls, "
                "       COALESCE(SUM(total_tokens), 0) AS tokens, "
                "       COALESCE(SUM(cost_usd), 0) AS cost "
                "  FROM openai_usage "
                " WHERE created_at >= NOW() - INTERVAL '7 days' "
                " GROUP BY kind ORDER BY cost DESC"
            )
            header = "Last 7d (all chats):"
        if not rows:
            await msg.reply("No usage recorded in that window.", disable_notification=True)
            return
        lines = [header]
        total = 0.0
        for r in rows:
            c = float(r["cost"] or 0)
            total += c
            lines.append(
                f"  {r['kind']:<10}  {r['calls']:>5} calls  "
                f"{int(r['tokens']):>8} tokens  ${c:.4f}"
            )
        lines.append(f"  TOTAL: ${total:.4f}")
        await msg.reply("\n".join(lines), disable_notification=True)

    async def _send_facts_for(target: int, reply_to: Message | None,
                              edit_in: Message | None) -> None:
        """Format and send the fact list for one chat.

        Either replies to `reply_to` (typed-arg path) or edits `edit_in`
        in place (callback path). Appends a cross-link action row.
        """
        facts = await rt.memory.list_facts(target, limit=50)
        if not facts:
            body = f"No facts stored for chat {target}."
        else:
            head = f"Facts for chat {target} ({len(facts)}):\n"
            body = head + "\n".join(f"[{f.id}] {f.fact}" for f in facts)
        body = body[:4000]
        kb = _mfx_keyboard(target)
        if reply_to:
            await reply_to.reply(
                body, reply_markup=kb, disable_notification=True,
            )
        elif edit_in:
            try:
                await edit_in.edit_text(body, reply_markup=kb)
            except TelegramBadRequest:
                pass

    async def _do_force_summarize_render(
        target: int,
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        """Run force_summarize and return (body, kb) for placeholder rendering."""
        report = await force_summarize(
            rt.memory, rt.openai, rt.settings, target,
        )
        body = _render_summary_report(report, target)
        kb = _mfx_keyboard(target)
        return body, kb

    @r.message(Command("memory_facts"))
    async def memory_facts(msg: Message) -> None:
        """Inspect durable facts. /memory_facts opens a chat picker;
        /memory_facts <chat_id> jumps straight to that chat."""
        if not await require_admin(msg, admin_ids):
            return
        args = (msg.text or "").split()
        if len(args) >= 2:
            try:
                target = int(args[1])
            except ValueError:
                await msg.reply("Invalid chat id.", disable_notification=True)
                return
            await _send_facts_for(target, reply_to=msg, edit_in=None)
            return
        chats = await rt.chats.list_known()
        kb = _chat_picker(
            chats, "mfacts",
            paginate=True,
            admin_user_id=msg.from_user.id if msg.from_user else None,
        )
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat to show stored facts for:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("mfacts:"))
    async def on_memory_facts(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parsed = _parse_picker_cb(cb.data)
        if parsed is None:
            await cb.answer(_expired("memory_facts"), show_alert=True)
            return
        kind, n = parsed
        if kind == "page":
            chats = await rt.chats.list_known()
            kb = _chat_picker(
                chats, "mfacts", paginate=True, page=n,
                admin_user_id=cb.from_user.id if cb.from_user else None,
            )
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        target = n
        if cb.from_user is not None:
            _LAST_PICKED_CHAT[cb.from_user.id] = (target, time.time())
        await _send_facts_for(target, reply_to=None, edit_in=cb.message)
        await cb.answer()

    @r.message(Command("memory_facts_all"))
    async def memory_facts_all(msg: Message) -> None:
        """Dump every fact across every known chat, grouped by chat."""
        if not await require_admin(msg, admin_ids):
            return
        chats = await rt.chats.list_known()
        if not chats:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        sections: list[str] = []
        total_facts = 0
        chats_with_facts = 0
        for c in chats:
            facts = await rt.memory.list_facts(c["chat_id"], limit=100)
            if not facts:
                continue
            chats_with_facts += 1
            total_facts += len(facts)
            head = f"━ {_chat_label(c)} [{c['chat_id']}] ({len(facts)}):"
            body = "\n".join(f"  [{f.id}] {f.fact}" for f in facts)
            sections.append(f"{head}\n{body}")
        if not sections:
            await msg.reply(
                "No facts stored in any known chat yet.",
                disable_notification=True,
            )
            return
        footer = (
            f"\n\nTotal: {total_facts} facts across "
            f"{chats_with_facts} chat(s)."
        )
        # Telegram caps outbound at 4096; chunk sections so each reply fits.
        chunks: list[str] = []
        current = ""
        for section in sections:
            piece = ("\n\n" if current else "") + section
            if len(current) + len(piece) > 3900:
                chunks.append(current)
                current = section
            else:
                current += piece
        if current:
            chunks.append(current)
        # Footer attaches to the last chunk if it fits, else gets its own.
        if chunks and len(chunks[-1]) + len(footer) <= 3900:
            chunks[-1] += footer
        else:
            chunks.append(footer.lstrip())
        for chunk in chunks:
            await msg.reply(chunk, disable_notification=True)

    @r.message(Command("memory_stats"))
    async def memory_stats(msg: Message) -> None:
        """Picker → per-chat memory diagnostics: counts, freshness,
        embedding coverage, summary state."""
        if not await require_admin(msg, admin_ids):
            return
        chats = await rt.chats.list_known()
        kb = _chat_picker(
            chats, "mstats",
            paginate=True,
            admin_user_id=msg.from_user.id if msg.from_user else None,
        )
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat for memory stats:",
            reply_markup=kb,
            disable_notification=True,
        )

    async def _render_memory_stats_body(
        target: int,
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Build the per-chat memory-stats body + cross-link keyboard.

        Extracted from on_memory_stats so the mfx:stats callback can
        reuse it.
        """
        # Per-chat aggregates via direct SQL (admin-only diagnostic, not a
        # hot path; cheaper than adding a half-dozen one-off repo methods).
        msg_rows = await rt.db.fetch(
            "SELECT role, COUNT(*) AS n, MIN(created_at) AS oldest, "
            "MAX(created_at) AS newest FROM messages "
            "WHERE chat_id = $1 GROUP BY role",
            target,
        )
        emb_rows = await rt.db.fetch(
            "SELECT ref_kind, COUNT(*) AS n FROM embeddings "
            "WHERE chat_id = $1 GROUP BY ref_kind",
            target,
        )
        fact_count = await rt.db.fetchval(
            "SELECT COUNT(*) FROM facts WHERE chat_id = $1", target,
        )
        summary_row = await rt.db.fetchrow(
            "SELECT COUNT(*) AS n, MAX(created_at) AS newest "
            "FROM summaries WHERE chat_id = $1",
            target,
        )
        latest_summary = await rt.memory.latest_summary(target)
        msg_total = sum(r["n"] for r in msg_rows) or 0
        msg_embed_count = next(
            (r["n"] for r in emb_rows if r["ref_kind"] == "message"), 0,
        )
        coverage = (msg_embed_count / msg_total * 100) if msg_total else 0
        roles_line = ", ".join(
            f"{r['role']}={r['n']}" for r in msg_rows
        ) or "(none)"
        oldest = min(
            (r["oldest"] for r in msg_rows if r["oldest"]), default=None,
        )
        newest = max(
            (r["newest"] for r in msg_rows if r["newest"]), default=None,
        )
        emb_breakdown = ", ".join(
            f"{r['ref_kind']}={r['n']}" for r in emb_rows
        ) or "(none)"
        next_summary_at = rt.settings.summary_trigger_messages
        if latest_summary:
            since_last_summary = await rt.memory.messages.count_since(
                target, latest_summary.covers_until_id,
            )
        else:
            since_last_summary = msg_total
        lines = [
            f"📊 Memory stats — chat {target}",
            "",
            f"Messages: {msg_total} ({roles_line})",
            f"  oldest: {oldest}",
            f"  newest: {newest}",
            "",
            f"Facts: {fact_count}",
            f"Summaries: {summary_row['n']} (latest at {summary_row['newest']})",
            f"  next auto-summarize in: "
            f"{max(0, next_summary_at - since_last_summary)} message(s)"
            f"  (have {since_last_summary} since last)",
            "",
            f"Embeddings: {emb_breakdown}",
            f"  message embedding coverage: {coverage:.1f}% "
            f"({msg_embed_count}/{msg_total})",
            f"  pgvector available: {rt.memory.pgvector_available}",
            f"  embedding model: {rt.openai.embedding_model} "
            f"(dim={rt.openai.embedding_dim})",
        ]
        body = "\n".join(lines)
        markup = _mst_keyboard(target)
        return body, markup

    async def _show_duckstats_user_picker(
        *,
        reply_to: Message | None = None,
        edit_in: Message | None = None,
        chat_id: int,
    ) -> None:
        """Render the user-picker for the duckstats editor."""
        rows = await _top_users_for_chat(rt.db, chat_id, limit=20)
        if not rows:
            body = f"No duck_stats rows for chat {chat_id}."
            kb = None
        else:
            body = (
                f"Top {len(rows)} duckhunters in chat {chat_id}. "
                "Tap one to edit:"
            )
            kb = _user_picker(rows, action=f"dse:user:{chat_id}")
        if reply_to:
            await reply_to.reply(
                body, reply_markup=kb, disable_notification=True,
            )
        elif edit_in:
            try:
                await edit_in.edit_text(body, reply_markup=kb)
            except TelegramBadRequest:
                pass

    async def _render_duckstat_editor(
        chat_id: int, user_id: int,
    ) -> tuple[str, InlineKeyboardMarkup] | None:
        row = await rt.db.fetchrow(
            "SELECT display_name, points, killed, befriended, misses, streak, best_streak "
            "  FROM duck_stats WHERE chat_id = $1 AND user_id = $2",
            chat_id, user_id,
        )
        if row is None:
            return None
        head = (
            f"📊 {row['display_name']} in chat {chat_id}\n\n"
            f"  points: {row['points']}    streak: {row['streak']}\n"
            f"  killed: {row['killed']}    befriended: {row['befriended']}\n"
            f"  misses: {row['misses']}    best_streak: {row['best_streak']}\n\n"
            "Tap a field to edit:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="points",      callback_data=f"dse:field:{chat_id}:{user_id}:points"),
                InlineKeyboardButton(text="streak",      callback_data=f"dse:field:{chat_id}:{user_id}:streak"),
            ],
            [
                InlineKeyboardButton(text="killed",      callback_data=f"dse:field:{chat_id}:{user_id}:killed"),
                InlineKeyboardButton(text="befriended",  callback_data=f"dse:field:{chat_id}:{user_id}:befriended"),
            ],
            [
                InlineKeyboardButton(text="misses",      callback_data=f"dse:field:{chat_id}:{user_id}:misses"),
                InlineKeyboardButton(text="best_streak", callback_data=f"dse:field:{chat_id}:{user_id}:best_streak"),
            ],
            [InlineKeyboardButton(text="⚠️ Reset entire row", callback_data=f"dse:reset:{chat_id}:{user_id}")],
            [InlineKeyboardButton(text="← back to users",     callback_data=f"dse:userlist:{chat_id}")],
        ])
        return head, kb

    @r.callback_query(F.data.startswith("mstats:"))
    async def on_memory_stats(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parsed = _parse_picker_cb(cb.data)
        if parsed is None:
            await cb.answer(_expired("memory_stats"), show_alert=True)
            return
        kind, n = parsed
        if kind == "page":
            chats = await rt.chats.list_known()
            kb = _chat_picker(
                chats, "mstats", paginate=True, page=n,
                admin_user_id=cb.from_user.id if cb.from_user else None,
            )
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        target = n
        if cb.from_user is not None:
            _LAST_PICKED_CHAT[cb.from_user.id] = (target, time.time())
        body, markup = await _render_memory_stats_body(target)
        if cb.message:
            try:
                await cb.message.edit_text(body[:4000], reply_markup=markup)
            except TelegramBadRequest:
                pass
        await cb.answer()

    @r.message(Command("memory_summary"))
    async def memory_summary(msg: Message) -> None:
        """Picker → show the latest stored summary for the chat."""
        if not await require_admin(msg, admin_ids):
            return
        chats = await rt.chats.list_known()
        kb = _chat_picker(
            chats, "msum",
            paginate=True,
            admin_user_id=msg.from_user.id if msg.from_user else None,
        )
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat to show its latest summary:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("msum:"))
    async def on_memory_summary(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parsed = _parse_picker_cb(cb.data)
        if parsed is None:
            await cb.answer(_expired("memory_summary"), show_alert=True)
            return
        kind, n = parsed
        if kind == "page":
            chats = await rt.chats.list_known()
            kb = _chat_picker(
                chats, "msum", paginate=True, page=n,
                admin_user_id=cb.from_user.id if cb.from_user else None,
            )
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        target = n
        if cb.from_user is not None:
            _LAST_PICKED_CHAT[cb.from_user.id] = (target, time.time())
        summary = await rt.memory.latest_summary(target)
        if summary is None:
            body = f"No summary stored for chat {target} yet."
        else:
            head = (
                f"Latest summary for chat {target}\n"
                f"id={summary.id} covers_until={summary.covers_until_id} "
                f"at {summary.created_at}\n\n"
            )
            body = head + summary.summary
        if cb.message:
            try:
                await cb.message.edit_text(body[:4000])
            except TelegramBadRequest:
                pass
        await cb.answer()

    @r.message(Command("memory_summarize_now"))
    async def memory_summarize_now(msg: Message) -> None:
        """Picker → force a summarization + fact-extraction pass on a chat,
        ignoring the message-count threshold."""
        if not await require_admin(msg, admin_ids):
            return
        chats = await rt.chats.list_known()
        kb = _chat_picker(
            chats, "mforce",
            paginate=True,
            admin_user_id=msg.from_user.id if msg.from_user else None,
        )
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat to force-summarize:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("mforce:"))
    async def on_memory_summarize_now(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parsed = _parse_picker_cb(cb.data)
        if parsed is None:
            await cb.answer(_expired("memory_summarize_now"), show_alert=True)
            return
        kind, n = parsed
        if kind == "page":
            chats = await rt.chats.list_known()
            kb = _chat_picker(
                chats, "mforce", paginate=True, page=n,
                admin_user_id=cb.from_user.id if cb.from_user else None,
            )
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        target = n
        if cb.from_user is not None:
            _LAST_PICKED_CHAT[cb.from_user.id] = (target, time.time())
        await cb.answer("Running summarizer…")
        await _with_working_placeholder(
            cb,
            f"Force-summarizing chat {target}…",
            lambda: _do_force_summarize_render(target),
        )

    @r.message(Command("memory_search"))
    async def memory_search(msg: Message) -> None:
        """Semantic-search the embedding store.

        /memory_search <query>             → picker, then search the chosen chat
        /memory_search <chat_id> <query>   → search that chat directly
        """
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split(None, 2)
        if len(parts) < 2:
            await msg.reply(
                "Usage: /memory_search <query>\n"
                "   or: /memory_search <chat_id> <query>",
                disable_notification=True,
            )
            return
        # Try the two-arg form first (chat_id then query).
        target: int | None = None
        query: str
        if len(parts) >= 3:
            try:
                target = int(parts[1])
                query = parts[2].strip()
            except ValueError:
                # `parts[1]` isn't an int → treat whole tail as the query.
                query = (parts[1] + " " + parts[2]).strip()
        else:
            query = parts[1].strip()
        if not query:
            await msg.reply("Empty query.", disable_notification=True)
            return
        if target is not None:
            await _run_memory_search(msg, target, query)
            return
        # No chat_id → stash the query and let the admin pick a chat.
        if msg.from_user is None:
            await msg.reply("Can't identify caller.", disable_notification=True)
            return
        _stash_search_query(msg.from_user.id, query)
        chats = await rt.chats.list_known()
        kb = _chat_picker(
            chats, "msrch",
            paginate=True,
            admin_user_id=msg.from_user.id,
        )
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            f"Search query stashed ({len(query)} chars). "
            "Pick a chat to search:",
            reply_markup=kb,
            disable_notification=True,
        )

    async def _run_memory_search(target_msg: Message, chat_id: int,
                                 query: str) -> None:
        hits = await rt.memory.semantic_search(chat_id, query, k=10)
        kb = _mst_keyboard(chat_id)
        if not hits:
            await target_msg.reply(
                f"No semantic hits in chat {chat_id} for "
                f"{query!r} (pgvector available: "
                f"{rt.memory.pgvector_available}).",
                reply_markup=kb,
                disable_notification=True,
            )
            return
        head = (
            f"🔎 Top {len(hits)} hits in chat {chat_id} for {query!r}:\n"
        )
        body_lines = []
        for h in hits:
            sim = h.get("similarity", 0)
            kind = h.get("ref_kind", "?")
            ref_id = h.get("ref_id", "?")
            content = (h.get("content") or "").replace("\n", " ")
            if len(content) > 220:
                content = content[:220] + "…"
            body_lines.append(
                f"  [{sim:.3f}] ({kind} #{ref_id}) {content}"
            )
        await target_msg.reply(
            (head + "\n".join(body_lines))[:4000],
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("msrch:"))
    async def on_memory_search(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parsed = _parse_picker_cb(cb.data)
        if parsed is None:
            await cb.answer(_expired("memory_search"), show_alert=True)
            return
        kind, n = parsed
        if kind == "page":
            chats = await rt.chats.list_known()
            kb = _chat_picker(
                chats, "msrch", paginate=True, page=n,
                admin_user_id=cb.from_user.id if cb.from_user else None,
            )
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        target = n
        if cb.from_user is None:
            await cb.answer("Can't identify caller.", show_alert=True)
            return
        _LAST_PICKED_CHAT[cb.from_user.id] = (target, time.time())
        query = _pop_search_query(cb.from_user.id)
        if query is None:
            await cb.answer(
                _expired("memory_search"),
                show_alert=True,
            )
            return
        await cb.answer("Searching…")
        # Replace the picker with a placeholder while we work.
        if cb.message:
            try:
                await cb.message.edit_text(
                    f"Searching chat {target} for {query!r}…",
                )
            except TelegramBadRequest:
                pass
            await _run_memory_search(cb.message, target, query)

    @r.message(Command("memory_forget"))
    async def memory_forget(msg: Message) -> None:
        """Delete a single durable fact by id. Admin-only."""
        if not await require_admin(msg, admin_ids):
            return
        args = (msg.text or "").split()
        if len(args) < 2:
            await msg.reply("Usage: /memory_forget <fact_id>", disable_notification=True)
            return
        try:
            fid = int(args[1])
        except ValueError:
            await msg.reply("Invalid fact id.", disable_notification=True)
            return
        await rt.memory.delete_fact(fid)
        await msg.reply(f"Deleted fact {fid}.", disable_notification=True)

    @r.message(Command("memory_wipe"))
    async def memory_wipe(msg: Message) -> None:
        """Erase a chat's stored conversation so a new persona takes hold.

        The bot's own past replies (stored as assistant messages), the
        running summary, and the semantic-search embeddings all feed back
        into the context window — so an old persona keeps leaking through
        even after /master_prompt is changed. This wipes that precedent.

        Usage:
          /memory_wipe                 → wipe THIS chat's conversation memory
          /memory_wipe <chat_id>       → wipe that chat's memory
          /memory_wipe <chat_id> facts → also delete durable facts
          /memory_wipe facts           → this chat, including facts

        Durable facts are kept by default (they're usually about people,
        not the bot's voice). Add 'facts' to nuke those too. Does NOT
        touch duck stats, quotes, or chat config.
        """
        if not await require_admin(msg, admin_ids):
            return
        args = (msg.text or "").split()[1:]
        include_facts = False
        target = msg.chat.id
        for a in args:
            if a.lower() == "facts":
                include_facts = True
            else:
                try:
                    target = int(a)
                except ValueError:
                    await msg.reply(
                        "Usage: /memory_wipe [chat_id] [facts]",
                        disable_notification=True,
                    )
                    return
        counts = await rt.memory.wipe_conversation(
            target, include_facts=include_facts,
        )
        parts = [f"{v} {k}" for k, v in counts.items()]
        await msg.reply(
            f"🧹 Wiped chat {target}'s conversation memory: "
            f"{', '.join(parts)}.\n"
            "The next reply starts fresh on the current persona. "
            + ("(Durable facts were cleared too.)" if include_facts else
               "(Durable facts kept — add 'facts' to clear those too.)"),
            disable_notification=True,
        )

    # ---------------------------------------------------------------- duckstats reset
    async def _render_chat_user_resetter(target: int) -> tuple[str, InlineKeyboardMarkup | None]:
        """Build the leaderboard-style picker used to choose a user (or
        'all') to reset within one chat. Returns (text, keyboard)."""
        rows = await _top_users_for_chat(rt.db, target, limit=20)
        if not rows:
            return (
                f"No duck stats stored for chat {target}.",
                None,
            )
        kb = _user_picker(
            rows, action=f"dsru:{target}",
            with_reset_all_for_chat_id=target,
        )
        head = (
            f"Top {len(rows)} duckhunters in chat {target}.\n"
            "Tap one to reset their stats, or 'Reset ALL' to clear the whole "
            "chat's leaderboard.\n\n"
            "(Friendships and named ducks live in duck_events and are NOT "
            "touched by this — only the duck_stats counters are cleared.)"
        )
        return head, kb

    async def _do_reset_user_stats(target: int, user_id: int) -> int:
        """Delete the duck_stats row. Returns 1 on hit, 0 on miss."""
        status = await rt.db.execute(
            "DELETE FROM duck_stats WHERE chat_id = $1 AND user_id = $2",
            target, user_id,
        )
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def _do_reset_all_in_chat(target: int) -> int:
        status = await rt.db.execute(
            "DELETE FROM duck_stats WHERE chat_id = $1", target,
        )
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError):
            return 0

    @r.message(Command("duckstats_reset"))
    async def duckstats_reset(msg: Message) -> None:
        """Reset duckhunt counters (admin-only).

        /duckstats_reset                       → chat picker → user picker
        /duckstats_reset <chat_id>             → user picker for that chat
        /duckstats_reset <chat_id> <user_id>   → direct reset of one user
        /duckstats_reset <chat_id> all         → wipe the whole chat's stats
        """
        if not await require_admin(msg, admin_ids):
            return
        parts = (msg.text or "").split()
        if len(parts) >= 3:
            try:
                chat_id = int(parts[1])
            except ValueError:
                await msg.reply("Invalid chat id.", disable_notification=True)
                return
            if parts[2].lower() == "all":
                n = await _do_reset_all_in_chat(chat_id)
                await msg.reply(
                    f"Wiped duck_stats for chat {chat_id} ({n} row(s) deleted).",
                    disable_notification=True,
                )
                return
            ref = parts[2]
            user_id = await _resolve_user_id(rt, chat_id, ref)
            if user_id is None:
                await msg.reply(
                    f"User '{ref}' not found in chat {chat_id}. "
                    "Use the numeric Telegram id, @username, or display name.",
                    disable_notification=True,
                )
                return
            n = await _do_reset_user_stats(chat_id, user_id)
            await msg.reply(
                f"Reset duck_stats for user {user_id} in chat {chat_id} "
                f"({n} row(s) deleted).",
                disable_notification=True,
            )
            return
        if len(parts) == 2:
            try:
                chat_id = int(parts[1])
            except ValueError:
                await msg.reply("Invalid chat id.", disable_notification=True)
                return
            text, kb = await _render_chat_user_resetter(chat_id)
            await msg.reply(
                text, reply_markup=kb, disable_notification=True,
            )
            return
        # No args → chat picker first.
        chats = await rt.chats.list_known()
        kb = _chat_picker(
            chats, "dsr",
            paginate=True,
            admin_user_id=msg.from_user.id if msg.from_user else None,
        )
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat to manage duckhunt stats for:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("dsr:"))
    async def on_dsr_chat_picked(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parsed = _parse_picker_cb(cb.data)
        if parsed is None:
            await cb.answer(_expired("duckstats_reset"), show_alert=True)
            return
        kind, n = parsed
        if kind == "page":
            chats = await rt.chats.list_known()
            kb = _chat_picker(
                chats, "dsr", paginate=True, page=n,
                admin_user_id=cb.from_user.id if cb.from_user else None,
            )
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        target = n
        if cb.from_user is not None:
            _LAST_PICKED_CHAT[cb.from_user.id] = (target, time.time())
        text, kb = await _render_chat_user_resetter(target)
        if cb.message:
            try:
                await cb.message.edit_text(text, reply_markup=kb)
            except TelegramBadRequest:
                pass
        await cb.answer()

    @r.callback_query(F.data.startswith("dsru:"))
    async def on_dsr_user_picked(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        try:
            _, chat_id_s, user_id_s = cb.data.split(":", 2)
            chat_id = int(chat_id_s)
            user_id = int(user_id_s)
        except (ValueError, IndexError):
            await cb.answer(_expired("duckstats_reset"), show_alert=True)
            return
        n = await _do_reset_user_stats(chat_id, user_id)
        body = (
            f"Reset duck_stats for user {user_id} in chat {chat_id} "
            f"({n} row(s) deleted)."
        )
        if cb.message:
            try:
                await cb.message.edit_text(body)
            except TelegramBadRequest:
                pass
        await cb.answer("Stats reset.")

    @r.callback_query(F.data.startswith("dsra:"))
    async def on_dsr_reset_all(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parts = cb.data.split(":")
        # dsra:CHATID  → prompt confirmation
        # dsra:CHATID:confirm | dsra:CHATID:cancel → execute / abort
        if len(parts) < 2:
            await cb.answer(_expired("duckstats_reset"), show_alert=True)
            return
        try:
            target = int(parts[1])
        except ValueError:
            await cb.answer(_expired("duckstats_reset"), show_alert=True)
            return
        if len(parts) == 2:
            count = await rt.db.fetchval(
                "SELECT COUNT(*) FROM duck_stats WHERE chat_id = $1", target,
            )
            body = (
                f"Wipe duck_stats for ALL {count} user(s) in chat {target}? "
                "This will reset everyone's points to 0."
            )
            if cb.message:
                try:
                    await cb.message.edit_text(
                        body, reply_markup=_confirmation_keyboard(f"dsra:{target}"),
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        suffix = parts[2]
        if suffix == "cancel":
            if cb.message:
                try:
                    await cb.message.edit_text("Cancelled. Nothing changed.")
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        # confirm
        n = await _do_reset_all_in_chat(target)
        if cb.message:
            try:
                await cb.message.edit_text(
                    f"Wiped duck_stats for chat {target} ({n} row(s) deleted)."
                )
            except TelegramBadRequest:
                pass
        await cb.answer("Chat wiped.")

    # ----------------------------------------------------- /duckstats_edit
    @r.message(Command("duckstats_edit"))
    async def duckstats_edit(msg: Message) -> None:
        """Interactive editor for one user's duckhunt stats.

        /duckstats_edit                       → chat picker → user picker → editor
        /duckstats_edit <chat_id>             → skip chat picker
        /duckstats_edit <chat_id> <user>      → direct editor (user = id, @username, or display name)
        """
        if not await require_admin(msg, admin_ids):
            return
        # Clear any stale parked custom-value entry for this admin.
        if msg.from_user is not None:
            _PENDING_CUSTOM_VALUES.pop(msg.from_user.id, None)
        parts = (msg.text or "").split()
        if len(parts) >= 3:
            try:
                chat_id = int(parts[1])
            except ValueError:
                await msg.reply("Invalid chat id.", disable_notification=True)
                return
            user_id = await _resolve_user_id(rt, chat_id, parts[2])
            if user_id is None:
                await msg.reply(
                    f"User '{parts[2]}' not found in chat {chat_id}.",
                    disable_notification=True,
                )
                return
            res = await _render_duckstat_editor(chat_id, user_id)
            if res is None:
                await msg.reply(
                    f"No duck_stats row for that user in chat {chat_id}.",
                    disable_notification=True,
                )
                return
            text, kb = res
            await msg.reply(
                text, reply_markup=kb, disable_notification=True,
            )
            return
        if len(parts) == 2:
            try:
                chat_id = int(parts[1])
            except ValueError:
                await msg.reply("Invalid chat id.", disable_notification=True)
                return
            await _show_duckstats_user_picker(reply_to=msg, chat_id=chat_id)
            return
        # No args → chat picker.
        chats = await rt.chats.list_known()
        kb = _chat_picker(
            chats, "dse:chatpick",
            paginate=True,
            admin_user_id=msg.from_user.id if msg.from_user else None,
        )
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat for duckstats editing:",
            reply_markup=kb, disable_notification=True,
        )

    @r.callback_query(F.data.startswith("dse:"))
    async def on_dse(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parts = cb.data.split(":")
        verb = parts[1] if len(parts) >= 2 else ""

        # Any dse navigation other than parking itself ("custom") means
        # the admin has moved on from whatever they previously parked, so
        # drop the entry now to prevent a later typed number from silently
        # writing to a field they've navigated away from.
        _pop_parked_on_navigation(
            verb, cb.from_user.id if cb.from_user is not None else None,
        )

        # dse:chatpick:CHATID  or  dse:chatpick:p:N
        if verb == "chatpick":
            if len(parts) >= 4 and parts[2] == "p":
                try:
                    page = int(parts[3])
                except ValueError:
                    await cb.answer(_expired("duckstats_edit"), show_alert=True)
                    return
                chats = await rt.chats.list_known()
                kb = _chat_picker(
                    chats, "dse:chatpick", paginate=True, page=page,
                    admin_user_id=cb.from_user.id if cb.from_user else None,
                )
                if cb.message:
                    try:
                        await cb.message.edit_reply_markup(reply_markup=kb)
                    except TelegramBadRequest:
                        pass
                await cb.answer()
                return
            try:
                target = int(parts[2])
            except (IndexError, ValueError):
                await cb.answer(_expired("duckstats_edit"), show_alert=True)
                return
            if cb.from_user is not None:
                _LAST_PICKED_CHAT[cb.from_user.id] = (target, time.time())
            await _show_duckstats_user_picker(
                edit_in=cb.message, chat_id=target,
            )
            await cb.answer()
            return

        # dse:userlist:CHATID
        if verb == "userlist":
            try:
                target = int(parts[2])
            except (IndexError, ValueError):
                await cb.answer(_expired("duckstats_edit"), show_alert=True)
                return
            await _show_duckstats_user_picker(
                edit_in=cb.message, chat_id=target,
            )
            await cb.answer()
            return

        # dse:user:CHATID:USERID — render editor for one user
        if verb == "user":
            try:
                chat_id = int(parts[2])
                user_id = int(parts[3])
            except (IndexError, ValueError):
                await cb.answer(_expired("duckstats_edit"), show_alert=True)
                return
            res = await _render_duckstat_editor(chat_id, user_id)
            if res is None:
                if cb.message:
                    try:
                        await cb.message.edit_text(
                            f"User {user_id} no longer has a duck_stats row.",
                        )
                    except TelegramBadRequest:
                        pass
                await cb.answer()
                return
            text, kb = res
            if cb.message:
                try:
                    await cb.message.edit_text(text, reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return

        # dse:field:CHATID:USERID:FIELD — open per-field delta picker
        if verb == "field":
            try:
                chat_id = int(parts[2])
                user_id = int(parts[3])
                field = parts[4]
            except (IndexError, ValueError):
                await cb.answer(_expired("duckstats_edit"), show_alert=True)
                return
            if field not in _DUCKSTAT_EDITABLE_FIELDS:
                await cb.answer("Bad field.", show_alert=True)
                return
            row = await rt.db.fetchrow(
                f"SELECT {field} FROM duck_stats WHERE chat_id=$1 AND user_id=$2",
                chat_id, user_id,
            )
            current = int(row[field]) if row else 0
            text, kb = _render_duckstat_field_picker(
                chat_id, user_id, field, current,
            )
            if cb.message:
                try:
                    await cb.message.edit_text(text, reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return

        # dse:delta:CHATID:USERID:FIELD:DELTA
        if verb == "delta":
            try:
                chat_id = int(parts[2])
                user_id = int(parts[3])
                field = parts[4]
                delta = int(parts[5])
            except (IndexError, ValueError):
                await cb.answer(_expired("duckstats_edit"), show_alert=True)
                return
            try:
                ok, new_val = await _apply_duckstat_delta(
                    rt.db, chat_id, user_id, field, delta,
                )
            except Exception as exc:
                # Wrap transient pool failures so the editor doesn't dead-end
                # on a silent traceback. The admin gets a real error toast.
                log.warning("dse:delta DB write failed: %s", exc)
                await cb.answer(f"DB error: {exc}", show_alert=True)
                return
            if not ok:
                # Row disappeared (concurrent reset, etc.). Bounce back to
                # the user picker so the admin isn't stuck on a stale view.
                await cb.answer(
                    "Row missing — bounced back to user list.",
                    show_alert=False,
                )
                await _show_duckstats_user_picker(
                    edit_in=cb.message, chat_id=chat_id,
                )
                return
            text, kb = _render_duckstat_field_picker(
                chat_id, user_id, field, new_val,
            )
            if cb.message:
                try:
                    await cb.message.edit_text(text, reply_markup=kb)
                    await cb.answer(f"{field} = {new_val}")
                except TelegramBadRequest:
                    # Likely a no-op edit (e.g. 0 → 0 clamp).
                    await cb.answer(f"{field} already at {new_val}.")
            else:
                await cb.answer(f"{field} = {new_val}")
            return

        # dse:zero:CHATID:USERID:FIELD
        if verb == "zero":
            try:
                chat_id = int(parts[2])
                user_id = int(parts[3])
                field = parts[4]
            except (IndexError, ValueError):
                await cb.answer(_expired("duckstats_edit"), show_alert=True)
                return
            try:
                ok, new_val = await _set_duckstat_field(
                    rt.db, chat_id, user_id, field, 0,
                )
            except Exception as exc:
                log.warning("dse:zero DB write failed: %s", exc)
                await cb.answer(f"DB error: {exc}", show_alert=True)
                return
            if not ok:
                await cb.answer(
                    "Row missing — bounced back to user list.",
                    show_alert=False,
                )
                await _show_duckstats_user_picker(
                    edit_in=cb.message, chat_id=chat_id,
                )
                return
            text, kb = _render_duckstat_field_picker(
                chat_id, user_id, field, new_val,
            )
            if cb.message:
                try:
                    await cb.message.edit_text(text, reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer(f"{field} set to 0.")
            return

        # dse:custom:CHATID:USERID:FIELD — park a wait-for-DM state
        if verb == "custom":
            try:
                chat_id = int(parts[2])
                user_id = int(parts[3])
                field = parts[4]
            except (IndexError, ValueError):
                await cb.answer(_expired("duckstats_edit"), show_alert=True)
                return
            if field not in _DUCKSTAT_EDITABLE_FIELDS:
                await cb.answer("Bad field.", show_alert=True)
                return
            if cb.from_user is None:
                await cb.answer("Can't identify caller.", show_alert=True)
                return
            _PENDING_CUSTOM_VALUES[cb.from_user.id] = (
                chat_id, user_id, field, time.time(),
            )
            if cb.message:
                try:
                    await cb.message.edit_text(
                        f"Send me the new value for `{field}` as your "
                        "next message. I'll wait up to 60 seconds. "
                        "(Send a non-number to cancel.)",
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return

        # dse:reset:CHATID:USERID — prompt confirmation
        # dse:reset:CHATID:USERID:confirm | dse:reset:CHATID:USERID:cancel
        if verb == "reset":
            try:
                chat_id = int(parts[2])
                user_id = int(parts[3])
            except (IndexError, ValueError):
                await cb.answer(_expired("duckstats_edit"), show_alert=True)
                return
            if len(parts) == 4:
                body = (
                    f"Reset duck_stats row for user {user_id} in chat {chat_id}? "
                    "This deletes points / streak / kills / etc."
                )
                if cb.message:
                    try:
                        await cb.message.edit_text(
                            body,
                            reply_markup=_confirmation_keyboard(
                                f"dse:reset:{chat_id}:{user_id}",
                            ),
                        )
                    except TelegramBadRequest:
                        pass
                await cb.answer()
                return
            suffix = parts[4]
            if suffix == "cancel":
                res = await _render_duckstat_editor(chat_id, user_id)
                if cb.message:
                    if res:
                        try:
                            await cb.message.edit_text(
                                res[0], reply_markup=res[1],
                            )
                        except TelegramBadRequest:
                            pass
                    else:
                        try:
                            await cb.message.edit_text("Cancelled.")
                        except TelegramBadRequest:
                            pass
                await cb.answer()
                return
            # confirm
            n = await _do_reset_user_stats(chat_id, user_id)
            await _show_duckstats_user_picker(
                edit_in=cb.message, chat_id=chat_id,
            )
            await cb.answer(f"Reset ({n} row).")
            return

        await cb.answer(_expired("duckstats_edit"), show_alert=True)

    # Custom-value DM handler. Triggered when the admin previously tapped
    # "Set to custom…" on the editor. Uses an F.func predicate so only
    # parked admins match it; other handlers are not blocked. The predicate
    # itself lives at module scope (see _has_parked_value) so unit tests can
    # exercise the TTL behaviour without building a Router.
    @r.message(F.func(_has_parked_value))
    async def on_admin_custom_value(msg: Message) -> None:
        if msg.from_user is None or not is_admin_user(msg.from_user.id, admin_ids):
            return
        entry = _PENDING_CUSTOM_VALUES.pop(msg.from_user.id, None)
        if entry is None:
            return
        chat_id, user_id, field, ts = entry
        if time.time() - ts > _CUSTOM_VALUE_TTL:
            await msg.reply(
                _expired("duckstats_edit"), disable_notification=True,
            )
            return
        try:
            new_value = int((msg.text or "").strip())
        except ValueError:
            await msg.reply(
                "That doesn't look like a number — cancelled.",
                disable_notification=True,
            )
            return
        # Clamp to Postgres INTEGER range here so we surface a friendly
        # message instead of letting an asyncpg DataError bubble up. The
        # helper does the same clamp as defense in depth.
        new_value = max(0, min(2_147_483_647, new_value))
        try:
            ok, new_val = await _set_duckstat_field(
                rt.db, chat_id, user_id, field, new_value,
            )
        except Exception as exc:
            log.warning("on_admin_custom_value DB write failed: %s", exc)
            await msg.reply(
                f"DB error while applying value: {exc}",
                disable_notification=True,
            )
            return
        if not ok:
            # No cb.message to re-render here (this is a fresh DM), so a
            # plain reply is the most we can do.
            await msg.reply(
                f"That user no longer has a duck_stats row in chat {chat_id}.",
                disable_notification=True,
            )
            return
        text, kb = _render_duckstat_field_picker(
            chat_id, user_id, field, new_val,
        )
        await msg.reply(text, reply_markup=kb, disable_notification=True)

    # ---------------------------------------------------------------- /manage
    @r.message(Command("manage"))
    async def manage(msg: Message) -> None:
        """One-screen admin hub. Pick a category to drill into."""
        if not await require_admin(msg, admin_ids):
            return
        await msg.reply(
            "⚙️ Admin hub. Pick a category:",
            reply_markup=_mgm_top_keyboard(),
            disable_notification=True,
        )

    async def _mgm_render_command_card(edit_in: Message, cmd: CatalogCommand) -> None:
        """Render the generic command card.

        The catalog row's usage line is wrapped in <code> so Telegram
        renders a tap-to-copy block — perfect for free-text commands
        that an inline keyboard can't gather arguments for.
        """
        # Escape only & < > so user-supplied catalog text can't break HTML;
        # the catalog is all admin-controlled but defensive escaping is
        # cheap.
        def esc(s: str) -> str:
            return (
                s.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;")
            )

        body = (
            f"<b>{esc(cmd.name)}</b>\n"
            f"{esc(cmd.desc)}\n"
            f"\n"
            f"<b>Usage</b> (tap to copy):\n"
            f"<code>{esc(cmd.usage)}</code>"
        )
        # Resolve which parent submenu to surface in the back row so the
        # admin lands one screen up instead of all the way at the hub.
        back_data = _mgm_cat_data(cmd.category)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="← back", callback_data=back_data),
            _home_button(),
        ]])
        try:
            await edit_in.edit_text(body, reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest:
            pass

    async def _mgm_render_status(edit_in: Message) -> None:
        """Render the Debug → Status panel."""
        # Memory diagnostics via existing repo APIs.
        chat_count = await rt.db.fetchval("SELECT COUNT(*) FROM chats")
        msg_total = await rt.db.fetchval("SELECT COUNT(*) FROM messages")
        fact_total = await rt.db.fetchval("SELECT COUNT(*) FROM facts")
        summary_total = await rt.db.fetchval("SELECT COUNT(*) FROM summaries")
        lines = [
            "🛠 Status",
            "",
            f"text provider: {rt.openai.text_provider}",
            f"  claude model: {rt.openai.claude_model}",
            f"  openai model: {rt.openai.text_model}",
            "",
            f"chats known: {chat_count}",
            f"messages: {msg_total}",
            f"facts: {fact_total}",
            f"summaries: {summary_total}",
            f"pgvector available: {rt.pgvector_available}",
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="← back", callback_data="mgm:debug"),
            _home_button(),
        ]])
        try:
            await edit_in.edit_text("\n".join(lines)[:4000], reply_markup=kb)
        except TelegramBadRequest:
            pass

    async def _mgm_render_cost(edit_in: Message) -> None:
        rows = await rt.db.fetch(
            "SELECT kind, COUNT(*) AS calls, "
            "       COALESCE(SUM(total_tokens), 0) AS tokens, "
            "       COALESCE(SUM(cost_usd), 0) AS cost "
            "  FROM openai_usage "
            " WHERE created_at >= NOW() - INTERVAL '7 days' "
            " GROUP BY kind ORDER BY cost DESC"
        )
        if not rows:
            body = "No usage recorded in the last 7 days."
        else:
            lines = ["Last 7d (all chats):"]
            total = 0.0
            for r in rows:
                c = float(r["cost"] or 0)
                total += c
                lines.append(
                    f"  {r['kind']:<10}  {r['calls']:>5} calls  "
                    f"{int(r['tokens']):>8} tokens  ${c:.4f}"
                )
            lines.append(f"  TOTAL: ${total:.4f}")
            body = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="← back", callback_data="mgm:debug"),
            _home_button(),
        ]])
        try:
            await edit_in.edit_text(body[:4000], reply_markup=kb)
        except TelegramBadRequest:
            pass

    async def _mgm_render_cmdlog(edit_in: Message) -> None:
        rows = await rt.command_log.tail(30)
        if not rows:
            body = "No command log entries yet."
        else:
            lines = [
                f"{r['created_at']:%H:%M:%S} chat={r['chat_id']} "
                f"user={r['user_id']} {r['command']} ok={r['success']}"
                + (f" err={r['error']}" if r["error"] else "")
                for r in rows
            ]
            body = "Recent commands:\n" + "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="← back", callback_data="mgm:debug"),
            _home_button(),
        ]])
        try:
            await edit_in.edit_text(body[:4000], reply_markup=kb)
        except TelegramBadRequest:
            pass

    async def _mgm_render_logs(edit_in: Message) -> None:
        lines = recent_log_lines(limit=50, contains=None)
        if not lines:
            body = "No log lines yet."
        else:
            body = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="← back", callback_data="mgm:debug"),
            _home_button(),
        ]])
        try:
            await edit_in.edit_text(body[:4000], reply_markup=kb)
        except TelegramBadRequest:
            pass

    async def _mgm_render_chat_list(edit_in: Message) -> None:
        rows = await rt.chats.list_known()
        if not rows:
            body = "No known chats yet."
        else:
            lines = [
                f"{r['chat_id']:>15} {r['type']:<10} {r['title'] or ''}"
                for r in rows
            ]
            body = "Known chats:\n" + "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="← back", callback_data="mgm:chats"),
            _home_button(),
        ]])
        try:
            await edit_in.edit_text(body[:4000], reply_markup=kb)
        except TelegramBadRequest:
            pass

    async def _mgm_render_ai_show(edit_in: Message) -> None:
        body = (
            f"Current text provider: {rt.openai.text_provider}\n"
            f"  claude model: {rt.openai.claude_model}\n"
            f"  openai model: {rt.openai.text_model}\n"
            "Switch with the buttons below."
        )
        try:
            await edit_in.edit_text(
                body,
                reply_markup=_aip_keyboard(),
            )
        except TelegramBadRequest:
            pass

    async def _mgm_spawn_all_now(edit_in: Message) -> None:
        chat_ids = await duckhunt_enabled_chat_ids(rt.db)
        if not chat_ids:
            body = "No chats have duckhunt enabled."
        else:
            spawned, skipped, failed = 0, 0, 0
            for chat_id in chat_ids:
                if await rt.duckhunt.active_duck(chat_id):
                    skipped += 1
                    continue
                try:
                    duck = await rt.duckhunt.spawn_duck(
                        chat_id, rt.settings.duckhunt_duck_lifetime_seconds,
                    )
                    text = await build_quack_message(rt.openai, duck.rarity)
                    await rt.bot.send_message(
                        chat_id, text, disable_notification=True,
                    )
                    spawned += 1
                except Exception as exc:
                    failed += 1
                    log.warning(
                        "mgm spawn_all failed for chat %s: %s", chat_id, exc,
                    )
            body = (
                f"Quacked in {spawned} chat(s). "
                f"Skipped {skipped} (duck already active). "
                f"Failed {failed}."
            )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← back", callback_data="mgm:duck")],
        ])
        try:
            await edit_in.edit_text(body[:4000], reply_markup=kb)
        except TelegramBadRequest:
            pass

    @r.callback_query(F.data.startswith("mgm:"))
    async def on_mgm(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        data = cb.data
        msg = cb.message
        # Top-level navigation.
        if data == "mgm:top":
            if msg:
                try:
                    await msg.edit_text(
                        "⚙️ Admin hub. Pick a category:",
                        reply_markup=_mgm_top_keyboard(),
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        if data == "mgm:memory":
            if msg:
                try:
                    await msg.edit_text(
                        "💾 Memory:",
                        reply_markup=_mgm_memory_submenu(),
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        if data == "mgm:duck":
            if msg:
                try:
                    await msg.edit_text(
                        "🦆 Duckhunt:",
                        reply_markup=_mgm_duck_submenu(),
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        if data == "mgm:ai":
            if msg:
                try:
                    await msg.edit_text(
                        "🤖 AI providers:",
                        reply_markup=_mgm_ai_submenu(),
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        if data == "mgm:chats":
            if msg:
                try:
                    await msg.edit_text(
                        "💬 Chats:",
                        reply_markup=_mgm_chats_submenu(),
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        if data == "mgm:debug":
            if msg:
                try:
                    await msg.edit_text(
                        "🛠 Debug & status:",
                        reply_markup=_mgm_debug_submenu(),
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        # New top-level categories (basics, ai, quotes, reminders, dm,
        # mod) and any future ones — the legacy five above use shorter
        # callback names for backwards compat.
        if data.startswith("mgm:cat:"):
            cat_key = data.split(":", 2)[2]
            cat = category_by_key(cat_key)
            if msg and cat:
                try:
                    await msg.edit_text(
                        f"{cat.label}\n{cat.blurb}",
                        reply_markup=_mgm_category_submenu(cat_key),
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        # Command card — generic renderer for any catalog row WITHOUT a
        # wired action. Shows the description plus a copyable
        # <code>/usage</code> hint so the admin can grab the form,
        # paste it, and fill in args.
        if data.startswith("mgm:cmd:"):
            slug = data.split(":", 2)[2]
            cmd = command_by_slug(slug)
            if msg and cmd:
                await _mgm_render_command_card(msg, cmd)
            await cb.answer()
            return

        # Memory leaves — each opens a chat picker that fires the
        # corresponding existing slash-command callback prefix.
        if data == "mgm:memory:facts":
            await _open_picker(
                rt, edit_in=msg,
                prompt="Pick a chat to show stored facts for:",
                action="mfacts",
                admin_user_id=cb.from_user.id if cb.from_user else None,
            home_to_manage=True,
            )
            await cb.answer()
            return
        if data == "mgm:memory:stats":
            await _open_picker(
                rt, edit_in=msg,
                prompt="Pick a chat for memory stats:",
                action="mstats",
                admin_user_id=cb.from_user.id if cb.from_user else None,
            home_to_manage=True,
            )
            await cb.answer()
            return
        if data == "mgm:memory:summary":
            await _open_picker(
                rt, edit_in=msg,
                prompt="Pick a chat to show its latest summary:",
                action="msum",
                admin_user_id=cb.from_user.id if cb.from_user else None,
            home_to_manage=True,
            )
            await cb.answer()
            return
        if data == "mgm:memory:force":
            await _open_picker(
                rt, edit_in=msg,
                prompt="Pick a chat to force-summarize:",
                action="mforce",
                admin_user_id=cb.from_user.id if cb.from_user else None,
            home_to_manage=True,
            )
            await cb.answer()
            return

        # Duckhunt leaves.
        if data == "mgm:duck:edit":
            await _open_picker(
                rt, edit_in=msg,
                prompt="Pick a chat for duckstats editing:",
                action="dse:chatpick",
                admin_user_id=cb.from_user.id if cb.from_user else None,
            home_to_manage=True,
            )
            await cb.answer()
            return
        if data == "mgm:duck:reset":
            await _open_picker(
                rt, edit_in=msg,
                prompt="Pick a chat to manage duckhunt stats for:",
                action="dsr",
                admin_user_id=cb.from_user.id if cb.from_user else None,
            home_to_manage=True,
            )
            await cb.answer()
            return
        if data == "mgm:duck:spawn":
            await _open_picker(
                rt, edit_in=msg,
                prompt="Pick a chat to spawn a duck in:",
                action="qchat",
                fetcher=lambda: duckhunt_enabled_chats(rt.db),
                admin_user_id=cb.from_user.id if cb.from_user else None,
                empty_message="No chats have duckhunt enabled.",
            )
            await cb.answer()
            return
        if data == "mgm:duck:spawnall":
            await cb.answer("Spawning…")
            if msg:
                await _mgm_spawn_all_now(msg)
            return

        # AI leaves.
        if data == "mgm:ai:show":
            if msg:
                await _mgm_render_ai_show(msg)
            await cb.answer()
            return

        # Chats leaves.
        if data == "mgm:chats:list":
            if msg:
                await _mgm_render_chat_list(msg)
            await cb.answer()
            return
        if data == "mgm:chats:pick":
            await _open_picker(
                rt, edit_in=msg,
                prompt="Pick a chat to copy its id:",
                action="pchat",
                admin_user_id=cb.from_user.id if cb.from_user else None,
            home_to_manage=True,
            )
            await cb.answer()
            return
        if data == "mgm:chats:config":
            await _open_picker(
                rt, edit_in=msg,
                prompt="Pick a chat to configure:",
                action="cfgfor",
                admin_user_id=cb.from_user.id if cb.from_user else None,
                home_to_manage=True,
            )
            await cb.answer()
            return
        if data == "mgm:chats:silenced":
            if msg:
                await _render_silenced_panel(msg)
            await cb.answer()
            return
        if data == "mgm:chats:silenced:add":
            await _open_picker(
                rt, edit_in=msg,
                prompt="Pick a chat to silence:",
                action="silch",
                admin_user_id=cb.from_user.id if cb.from_user else None,
                home_to_manage=False,
            )
            await cb.answer()
            return

        # Debug leaves.
        if data == "mgm:debug:status":
            if msg:
                await _mgm_render_status(msg)
            await cb.answer()
            return
        if data == "mgm:debug:logs":
            if msg:
                await _mgm_render_logs(msg)
            await cb.answer()
            return
        if data == "mgm:debug:cost":
            if msg:
                await _mgm_render_cost(msg)
            await cb.answer()
            return
        if data == "mgm:debug:cmdlog":
            if msg:
                await _mgm_render_cmdlog(msg)
            await cb.answer()
            return
        if data == "mgm:debug:toggles":
            if msg and cb.from_user is not None:
                state = debug_toggles_all_for(cb.from_user.id)
                lines = ["🎛 Debug toggles (yours, persisted across restarts):"]
                for name, on in state.items():
                    lines.append(f"  {name}: {'ON' if on else 'off'}")
                lines.append(
                    "\nFlip with: /debug_toggle <name> on|off"
                )
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="← back", callback_data="mgm:debug"),
                    _home_button(),
                ]])
                try:
                    await msg.edit_text("\n".join(lines)[:4000], reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        if data == "mgm:debug:cleard":
            await _open_picker(
                rt, edit_in=msg,
                prompt="Pick a chat to force-clear its active duck:",
                action="dcd",
                admin_user_id=cb.from_user.id if cb.from_user else None,
            home_to_manage=True,
            )
            await cb.answer()
            return

        await cb.answer("Unknown menu item.", show_alert=True)

    @r.callback_query(F.data == "noop")
    async def on_noop(cb: CallbackQuery) -> None:
        """No-op for picker pagination filler buttons."""
        await cb.answer()

    # ------------------------------------------------ cross-link dispatchers
    @r.callback_query(F.data.startswith("mst:"))
    async def on_mst(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parts = cb.data.split(":", 2)
        # mst:back | mst:facts:CHATID | mst:force:CHATID | mst:edit:CHATID
        if len(parts) >= 2 and parts[1] == "back":
            chats = await rt.chats.list_known()
            kb = _chat_picker(
                chats, "mstats", paginate=True,
                admin_user_id=cb.from_user.id if cb.from_user else None,
            )
            if cb.message:
                try:
                    await cb.message.edit_text(
                        "Pick a chat for memory stats:", reply_markup=kb,
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        if len(parts) < 3:
            await cb.answer(_expired("memory_stats"), show_alert=True)
            return
        verb = parts[1]
        try:
            target = int(parts[2])
        except ValueError:
            await cb.answer(_expired("memory_stats"), show_alert=True)
            return
        if verb == "facts":
            await _send_facts_for(target, reply_to=None, edit_in=cb.message)
            await cb.answer()
            return
        if verb == "force":
            await cb.answer("Running summarizer…")
            await _with_working_placeholder(
                cb,
                f"Force-summarizing chat {target}…",
                lambda: _do_force_summarize_render(target),
            )
            return
        if verb == "edit":
            await _show_duckstats_user_picker(
                reply_to=None, edit_in=cb.message, chat_id=target,
            )
            await cb.answer()
            return
        await cb.answer(_expired("memory_stats"), show_alert=True)

    @r.callback_query(F.data.startswith("mfx:"))
    async def on_mfx(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parts = cb.data.split(":", 2)
        # mfx:back | mfx:reextract:CHATID | mfx:stats:CHATID
        if len(parts) >= 2 and parts[1] == "back":
            chats = await rt.chats.list_known()
            kb = _chat_picker(
                chats, "mfacts", paginate=True,
                admin_user_id=cb.from_user.id if cb.from_user else None,
            )
            if cb.message:
                try:
                    await cb.message.edit_text(
                        "Pick a chat to show stored facts for:",
                        reply_markup=kb,
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        if len(parts) < 3:
            await cb.answer(_expired("memory_facts"), show_alert=True)
            return
        verb = parts[1]
        try:
            target = int(parts[2])
        except ValueError:
            await cb.answer(_expired("memory_facts"), show_alert=True)
            return
        if verb == "reextract":
            await cb.answer("Re-extracting…")
            await _with_working_placeholder(
                cb,
                f"Re-extracting facts for chat {target}…",
                lambda: _do_force_summarize_render(target),
            )
            return
        if verb == "stats":
            body, markup = await _render_memory_stats_body(target)
            if cb.message:
                try:
                    await cb.message.edit_text(
                        body[:4000], reply_markup=markup,
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        await cb.answer(_expired("memory_facts"), show_alert=True)

    @r.callback_query(F.data.startswith("aip:"))
    async def on_aip(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parts = cb.data.split(":", 3)
        # aip:switch:claude|openai
        # aip:list:claude|openai
        # aip:setmodel:claude|openai:<model>
        # aip:back
        if len(parts) < 2:
            await cb.answer(_expired("ai_provider"), show_alert=True)
            return
        verb = parts[1]
        if verb == "back":
            body = (
                f"Current text provider: {rt.openai.text_provider}\n"
                f"  claude model: {rt.openai.claude_model}\n"
                f"  openai model: {rt.openai.text_model}\n"
                "Switch with: /ai_provider claude  or  /ai_provider openai"
            )
            if cb.message:
                try:
                    await cb.message.edit_text(
                        body,
                        reply_markup=_aip_keyboard(),
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        if len(parts) < 3:
            await cb.answer(_expired("ai_provider"), show_alert=True)
            return
        arg = parts[2]
        if verb == "switch":
            if arg not in ("claude", "openai"):
                await cb.answer("Bad provider.", show_alert=True)
                return
            try:
                rt.openai.set_text_provider(arg)
            except ValueError as exc:
                await cb.answer(f"Can't switch: {exc}", show_alert=True)
                return
            await kv_set(rt.db, "text_provider", arg)
            body = (
                f"Current text provider: {rt.openai.text_provider}\n"
                f"  claude model: {rt.openai.claude_model}\n"
                f"  openai model: {rt.openai.text_model}"
            )
            if cb.message:
                try:
                    await cb.message.edit_text(
                        body,
                        reply_markup=_aip_keyboard(),
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer(f"Switched to {arg}.")
            return
        if verb == "list":
            if arg == "claude":
                models = _KNOWN_CLAUDE_TEXT_MODELS
            elif arg == "openai":
                models = _KNOWN_OPENAI_TEXT_MODELS
            else:
                await cb.answer("Bad provider.", show_alert=True)
                return
            kb_rows = [
                [InlineKeyboardButton(
                    text=m, callback_data=f"aip:setmodel:{arg}:{m}",
                )]
                for m in models
            ]
            kb_rows.append([InlineKeyboardButton(
                text="← back", callback_data="aip:back",
            )])
            if cb.message:
                try:
                    await cb.message.edit_text(
                        f"Pick a {arg} text model:",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        if verb == "setmodel":
            if arg not in ("claude", "openai") or len(parts) < 4:
                await cb.answer("Bad provider.", show_alert=True)
                return
            new_model = parts[3]
            if arg == "claude":
                rt.openai.set_claude_model(new_model)
                await kv_set(rt.db, "claude_text_model", new_model)
            else:
                rt.openai.set_openai_text_model(new_model)
                await kv_set(rt.db, "openai_text_model", new_model)
            body = (
                f"Current text provider: {rt.openai.text_provider}\n"
                f"  claude model: {rt.openai.claude_model}\n"
                f"  openai model: {rt.openai.text_model}\n\n"
                f"Set {arg} model to {new_model}."
            )
            if cb.message:
                try:
                    await cb.message.edit_text(
                        body,
                        reply_markup=_aip_keyboard(),
                    )
                except TelegramBadRequest:
                    pass
            await cb.answer(f"{arg} model set.")
            return
        await cb.answer(_expired("ai_provider"), show_alert=True)

    # ----------------------------------------------------- /debug_toggle
    @r.message(Command("debug_toggle"))
    async def debug_toggle(msg: Message) -> None:
        """View or flip admin-scoped debug toggles.

        Usage:
            /debug_toggle                       — print current state
            /debug_toggle show                  — same
            /debug_toggle <name> on|off|1|0     — flip one toggle
        """
        if not await require_admin(msg, admin_ids):
            return
        if msg.from_user is None:
            return
        parts = (msg.text or "").split()
        if len(parts) < 2 or parts[1].lower() == "show":
            state = debug_toggles_all_for(msg.from_user.id)
            lines = ["🎛 Debug toggles (yours, persisted across restarts):"]
            for name, on in state.items():
                lines.append(f"  {name}: {'ON' if on else 'off'}")
            lines.append("\nUsage: /debug_toggle <name> on|off")
            await msg.reply("\n".join(lines), disable_notification=True)
            return
        if len(parts) < 3:
            await msg.reply(
                "Usage: /debug_toggle <name> on|off",
                disable_notification=True,
            )
            return
        name = parts[1]
        if name not in DEBUG_TOGGLE_NAMES:
            await msg.reply(
                f"Unknown toggle '{name}'. Valid: {', '.join(DEBUG_TOGGLE_NAMES)}",
                disable_notification=True,
            )
            return
        val = parts[2].lower()
        on = val in ("on", "1", "true", "yes")
        await set_debug_toggle(rt.db, msg.from_user.id, name, on)
        await msg.reply(
            f"{name}: {'ON' if on else 'off'}",
            disable_notification=True,
        )

    # ----------------------------------------------------- /debug_clear_duck
    @r.message(Command("debug_clear_duck"))
    async def debug_clear_duck(msg: Message) -> None:
        """Picker → force-resolve the active duck for a chat.

        Useful when a stuck duck blocks `bef` flow testing or you want to
        re-trigger the spawner.
        """
        if not await require_admin(msg, admin_ids):
            return
        chats = await rt.chats.list_known()
        kb = _chat_picker(
            chats, "dcd",
            paginate=True,
            admin_user_id=msg.from_user.id if msg.from_user else None,
        )
        if kb is None:
            await msg.reply("No known chats yet.", disable_notification=True)
            return
        await msg.reply(
            "Pick a chat to force-clear its active duck:",
            reply_markup=kb,
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("dcd:"))
    async def on_debug_clear_duck(cb: CallbackQuery) -> None:
        if not await _gate_callback(cb):
            return
        parsed = _parse_picker_cb(cb.data)
        if parsed is None:
            await cb.answer(_expired("debug_clear_duck"), show_alert=True)
            return
        kind, n = parsed
        if kind == "page":
            chats = await rt.chats.list_known()
            kb = _chat_picker(
                chats, "dcd", paginate=True, page=n,
                admin_user_id=cb.from_user.id if cb.from_user else None,
            )
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=kb)
                except TelegramBadRequest:
                    pass
            await cb.answer()
            return
        target = n
        if cb.from_user is not None:
            _LAST_PICKED_CHAT[cb.from_user.id] = (target, time.time())
        res = await rt.db.execute(
            "UPDATE duck_events SET resolved = TRUE, "
            "       resolved_action = 'admin_cleared', "
            "       resolved_at = NOW() "
            " WHERE chat_id = $1 AND resolved = FALSE",
            target,
        )
        try:
            cleared = int(res.split()[-1])
        except (ValueError, IndexError):
            cleared = 0
        body = (
            f"Force-cleared active duck for chat {target} "
            f"({cleared} row(s) updated)."
        )
        if cb.message:
            try:
                await cb.message.edit_text(body)
            except TelegramBadRequest:
                pass
        await cb.answer("Cleared.")

    return r
