"""/newbot and friends: the owner adds and runs other bots from Dale's DM.

The registry and its rules are ipedro/bots.py; the process that actually
runs the bots is ipedro/supervisor.py. Everything here is owner-only, in
DM — a new Telegram account answering in your groups is a bigger deal
than anything an admin command does.

/newbot carries a bot token, so the message holding it is deleted on
sight, before anything else happens — including when it's posted in a
group by mistake, where the owner gets a DM saying so.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from ipedro import bots
from ipedro.auth import is_owner
from ipedro.handlers.common import auth_ctx
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

# The supervisor checks in every few seconds; this long without one means
# it isn't running.
_SUPERVISOR_STALE_SECONDS = 60

_PRIVACY_WARNING = (
    "Its Group Privacy is ON, so in groups it'll only see commands, "
    "replies to it and @mentions. Turn it off: @BotFather → /mybots → "
    "Bot Settings → Group Privacy → Turn off (then re-add it to any group "
    "it's already in)."
)

_NOT_DELETED = (
    "⚠️ I couldn't delete your message, and it has the token in it. "
    "Delete it yourself."
)


async def get_me(token: str):
    """Ask Telegram who a token belongs to. Module-level so tests can
    swap it out."""
    probe = Bot(token=token)
    try:
        return await probe.get_me()
    finally:
        await probe.session.close()


async def _delete(msg: Message) -> bool:
    try:
        await msg.delete()
        return True
    except Exception as exc:
        log.info("couldn't delete a message holding a bot token: %s", exc)
        return False


def _ago(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "never"
    secs = max(0, int((now - when).total_seconds()))
    if secs < 120:
        return f"{secs}s"
    if secs < 7200:
        return f"{secs // 60}m"
    if secs < 172800:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


async def _supervisor_line(rt: Runtime) -> str:
    seen = await bots.supervisor_last_seen(rt.db)
    now = datetime.now(timezone.utc)
    if seen is not None and (now - seen).total_seconds() <= _SUPERVISOR_STALE_SECONDS:
        return f"Supervisor checked in {_ago(seen, now)} ago."
    return (
        "⚠️ The supervisor isn't running"
        + (f" (last seen {_ago(seen, now)} ago)" if seen else "")
        + ", so nothing here starts or stops. It's the `bots` service in "
        "docker/docker-compose.yml."
    )


def _describe(b: bots.BotRow, now: datetime) -> str:
    head = f"#{b.id} {b.name} (@{b.username})"
    if b.status == "stopped":
        return f"{head}: stopped"
    if b.running:
        state = f"running {_ago(b.started_at, now)}"
    else:
        last = ""
        if b.last_exit:
            # "exited with code 1 after 6s — TelegramUnauthorizedError: …":
            # the header, and the traceback's last line, which says why.
            lines = b.last_exit.splitlines()
            last = lines[0].rstrip(":")
            if len(lines) > 1:
                last += " — " + lines[-1].strip()[:200]
            last = bots.scrub(last, b.token)  # already scrubbed; belt and braces
        state = "⚠️ not running" + (f": {last}" if last else " yet")
    if b.restarts:
        state += f", {b.restarts} restart{'s' if b.restarts != 1 else ''}"
    return f"{head}: {state}"


def build_router(rt: Runtime) -> Router:
    r = Router(name="bots")

    async def _owner(msg: Message) -> bool:
        """Owner, in DM. Not require_owner: that logs a refused command's
        text, and this text can hold a token."""
        ctx = auth_ctx(msg)
        if is_owner(ctx, rt.settings.owner_id):
            return True
        if ctx.chat_type == "private":
            # answer, not reply: /newbot may have just deleted msg.
            await msg.answer("This is owner-only.", disable_notification=True)
        else:
            log.warning(
                "Refused a bot-management command from user %s in chat %s",
                ctx.user_id, msg.chat.id if msg.chat else "?",
            )
        return False

    async def _pick(msg: Message, usage: str) -> bots.BotRow | None:
        parts = (msg.text or "").split(None, 1)
        if len(parts) < 2:
            await msg.reply(usage, disable_notification=True)
            return None
        found = await bots.resolve(rt.db, parts[1])
        if len(found) == 1:
            return found[0]
        if not found:
            await msg.reply(
                f"No bot called {parts[1].strip()!r}. /bots lists them.",
                disable_notification=True,
            )
        else:
            await msg.reply(
                "More than one bot goes by that: "
                + ", ".join(f"#{b.id} {b.name}" for b in found)
                + ". Use the #number.",
                disable_notification=True,
            )
        return None

    @r.message(Command("newbot"))
    async def newbot(msg: Message) -> None:
        text = msg.text or ""
        deleted = bots.contains_token(text) and await _delete(msg)
        ctx = auth_ctx(msg)
        if ctx.chat_type != "private" and bots.contains_token(text):
            if ctx.user_id == rt.settings.owner_id:
                try:
                    await rt.bot.send_message(
                        rt.settings.owner_id,
                        "You posted a bot token in a group"
                        + (", so I deleted it" if deleted else
                           " and I couldn't delete it. Delete it, and revoke "
                           "the token in @BotFather (/revoke)")
                        + ". /newbot only works here, in this DM.",
                    )
                except Exception as exc:
                    log.info("couldn't DM the owner about a leaked token: %s", exc)
            return
        if not await _owner(msg):
            return
        # The command's own message may be gone, so answer, don't reply.
        say = msg.answer if deleted else msg.reply
        try:
            req = bots.parse_newbot(text)
        except bots.NewBotError as exc:
            await say(
                str(exc) + ("" if deleted or not bots.contains_token(text)
                            else "\n\n" + _NOT_DELETED),
                disable_notification=True,
            )
            return
        tail = "" if deleted else "\n\n" + _NOT_DELETED
        if req.token == rt.settings.telegram_bot_token:
            await say("That's my own token." + tail, disable_notification=True)
            return
        try:
            me = await get_me(req.token)
        except Exception as exc:
            log.info("/newbot: getMe failed: %s", type(exc).__name__)
            await say(
                "Telegram didn't accept that token. Check it in @BotFather "
                "(/mybots → API Token)." + tail,
                disable_notification=True,
            )
            return
        row = await bots.register(
            rt.db, req, telegram_id=me.id, username=me.username or str(me.id),
            created_by=ctx.user_id,
        )
        if row is None:
            existing = await bots.find_by_telegram_id(rt.db, me.id)
            await say(
                f"@{me.username} is already bot #{existing.id} "
                f"({existing.status}). /bot_start, /bot_stop or "
                "/bot_remove it instead." + tail,
                disable_notification=True,
            )
            return
        lines = [
            f"Added {row.name} (@{row.username}) as bot #{row.id}, answering "
            f"to: {row.aliases}.",
            "Persona: " + (
                "set." if row.persona else
                f"none given, so it starts as plain '{row.name}'."
            ) + " DM it /master_prompt to change it any time.",
            await _supervisor_line(rt),
        ]
        if not getattr(me, "can_read_all_group_messages", True):
            lines.append(_PRIVACY_WARNING)
        await say("\n\n".join(lines) + tail, disable_notification=True)
        await rt.command_log.add(
            msg.chat.id, ctx.user_id, "/newbot",
            f"#{row.id} @{row.username}", True,
        )

    @r.message(Command("bots"))
    async def list_cmd(msg: Message) -> None:
        if not await _owner(msg):
            return
        rows = await bots.list_bots(rt.db)
        now = datetime.now(timezone.utc)
        body = (
            "\n".join(_describe(b, now) for b in rows) if rows
            else "No other bots yet. /newbot adds one."
        )
        await msg.reply(
            body + "\n\n" + await _supervisor_line(rt),
            disable_notification=True,
        )

    async def _set(msg: Message, status: str, verb: str) -> None:
        if not await _owner(msg):
            return
        row = await _pick(msg, f"Usage: /bot_{verb} <#number or name>")
        if row is None:
            return
        await bots.set_status(rt.db, row.id, status)
        done = {
            "active": "will start within a few seconds",
            "stopped": "will stop within a few seconds. /bot_start brings it back",
            "removed": (
                "is removed and will stop within a few seconds. Its memory "
                f"stays in database {bots.db_name_for(row.telegram_id)} "
                "until you drop it, and /newbot with its token brings it back"
            ),
        }[status]
        await msg.reply(f"#{row.id} {row.name} {done}.", disable_notification=True)
        await rt.command_log.add(
            msg.chat.id, msg.from_user.id, f"/bot_{verb}", f"#{row.id}", True,
        )

    @r.message(Command("bot_start"))
    async def start_cmd(msg: Message) -> None:
        await _set(msg, "active", "start")

    @r.message(Command("bot_stop"))
    async def stop_cmd(msg: Message) -> None:
        await _set(msg, "stopped", "stop")

    @r.message(Command("bot_remove"))
    async def remove_cmd(msg: Message) -> None:
        await _set(msg, "removed", "remove")

    return r
