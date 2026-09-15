"""/dalegif — curate the Dale Gribble GIF library from inside Telegram.

The whole point is that adding a GIF never involves a file, a URL or a code
change. Telegram's own GIF picker is Tenor, so:

    search "dale gribble" in the GIF picker → send it → reply /dalegif <tags>

and it's stored forever as a Telegram file_id. Tagging a GIF someone *else*
posted works the same way, which is why this is gated on ``is_admin_user``
(admin anywhere) rather than the DM-only ``require_admin``.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ipedro import dale_gifs as dg
from ipedro.auth import is_admin_user
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

# Telegram transcodes GIFs to MP4 and delivers them as `animation`; a .gif
# uploaded from the file picker arrives as a `document` instead.
_GIF_MIMES = frozenset({"image/gif", "video/mp4"})

_USAGE = (
    "Dale GIFs:\n"
    "  reply /dalegif <tags> to a GIF  — save it (tags like: pocketsand "
    "paranoia conspiracy)\n"
    "  /dalegif list [tag]             — what's stored\n"
    "  /dalegif test <tag>             — send one back\n"
    "  /dalegif remove <id>            — delete one\n"
    "  /dalegif seed                   — load the built-in starter set"
)


def _args(msg: Message) -> list[str]:
    """Command arguments. Reads caption too — a GIF sent with a caption is
    the one-step add path, and its text lives in `caption`, not `text`."""
    raw = msg.text or msg.caption or ""
    return raw.split()[1:]


def _sources(msg: Message):
    """This message, then the one it replies to."""
    yield msg
    reply = getattr(msg, "reply_to_message", None)
    if reply is not None:
        yield reply


def _find_gif(msg: Message) -> tuple[str, str] | None:
    """(file_id, file_unique_id) of a GIF on or replied-to by this message."""
    for src in _sources(msg):
        anim = getattr(src, "animation", None)
        if anim is not None:
            return anim.file_id, anim.file_unique_id
        doc = getattr(src, "document", None)
        if doc is not None and (getattr(doc, "mime_type", None) or "") in _GIF_MIMES:
            return doc.file_id, doc.file_unique_id
    return None


def _find_unusable(msg: Message) -> str | None:
    """Name of a media type we can't send back as an animation, if present.

    Telegram rejects a video/sticker file_id from sendAnimation outright, so
    say that plainly instead of storing something that can never be sent.
    """
    for src in _sources(msg):
        if getattr(src, "video", None) is not None:
            return "video"
        if getattr(src, "sticker", None) is not None:
            return "sticker"
        if getattr(src, "photo", None):
            return "photo"
    return None


def build_router(rt: Runtime) -> Router:
    r = Router(name="dale")
    admin_ids = rt.settings.admin_ids

    @r.message(Command("dalegif"))
    async def dalegif(msg: Message) -> None:
        # Silent for non-admins: same reasoning as the other admin commands,
        # bystanders don't need to learn the command exists.
        if msg.from_user is None or not is_admin_user(msg.from_user.id, admin_ids):
            return

        args = _args(msg)

        # A GIF in play always means "save this", whatever the args say —
        # otherwise tagging one `list` would be ambiguous.
        found = _find_gif(msg)
        if found is not None:
            await _add(rt, msg, args, *found)
            return

        sub = args[0].lower() if args else ""
        if sub == "list":
            await _list(rt, msg, args[1] if len(args) > 1 else None)
        elif sub == "test":
            await _test(rt, msg, args[1] if len(args) > 1 else "")
        elif sub == "remove":
            await _remove(rt, msg, args[1] if len(args) > 1 else "")
        elif sub == "seed":
            await _seed(rt, msg)
        else:
            unusable = _find_unusable(msg)
            if unusable:
                await msg.reply(
                    f"That's a {unusable}, not a GIF — Telegram won't let me "
                    "send it back as one. Send it as a GIF/animation.",
                    disable_notification=True,
                )
                return
            await msg.reply(_USAGE, disable_notification=True)

    return r


async def _add(
    rt: Runtime, msg: Message, args: list[str], file_id: str, file_unique_id: str,
) -> None:
    tags = dg.parse_tags(" ".join(args))
    if not tags:
        await msg.reply(
            "Give it at least one tag, e.g. /dalegif pocketsand paranoia",
            disable_notification=True,
        )
        return
    gif_id, was_new = await dg.add(
        rt.db, tags, file_id=file_id, file_unique_id=file_unique_id,
        added_by=msg.from_user.id if msg.from_user else None,
    )
    if gif_id is None:
        await msg.reply("Couldn't save that one.", disable_notification=True)
        return
    stored = [g for g in await dg.list_all(rt.db) if g.id == gif_id]
    now = ", ".join(stored[0].tags) if stored else ", ".join(tags)
    if was_new:
        await msg.reply(
            f"Filed. #{gif_id} — tags: {now}", disable_notification=True,
        )
    else:
        await msg.reply(
            f"Already had that one as #{gif_id}. Tags now: {now}",
            disable_notification=True,
        )


async def _list(rt: Runtime, msg: Message, tag: str | None) -> None:
    gifs = await dg.list_all(rt.db, tag)
    if not gifs:
        if tag:
            await msg.reply(
                f"Nothing tagged '{dg.normalize_tag(tag)}' yet.",
                disable_notification=True,
            )
            return
        await msg.reply(
            "Library's empty. Run /dalegif seed for the starter set, or reply "
            "/dalegif <tags> to any GIF.",
            disable_notification=True,
        )
        return
    lines = [f"🦆 Dale GIFs ({len(gifs)}):"]
    for g in gifs:
        source = "file" if g.file_id else "url"
        lines.append(
            f"  #{g.id}  {', '.join(g.tags) or '(untagged)'}  "
            f"[{source}, sent {g.send_count}x]"
        )
    await msg.reply("\n".join(lines)[:4000], disable_notification=True)


async def _test(rt: Runtime, msg: Message, tag: str) -> None:
    if not tag:
        await msg.reply("Usage: /dalegif test <tag>", disable_notification=True)
        return
    ok = await dg.send_random(
        rt.db, msg, tag,
        fallback=f"Nothing to send for '{dg.normalize_tag(tag)}' yet.",
    )
    if not ok:
        log.info("dale gif test produced no gif for tag %r", tag)


async def _remove(rt: Runtime, msg: Message, raw_id: str) -> None:
    try:
        gif_id = int(raw_id)
    except ValueError:
        await msg.reply(
            "Usage: /dalegif remove <id>  (ids come from /dalegif list)",
            disable_notification=True,
        )
        return
    gone = await dg.remove(rt.db, gif_id)
    await msg.reply(
        f"Deleted #{gif_id}." if gone else f"No GIF #{gif_id}.",
        disable_notification=True,
    )


async def _seed(rt: Runtime, msg: Message) -> None:
    added, skipped = await dg.apply_seed(rt.db)
    await msg.reply(
        f"Seeded: {added} added, {skipped} already there.",
        disable_notification=True,
    )
