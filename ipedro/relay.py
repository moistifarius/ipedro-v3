"""Dale passing a message on to another chat, if he chooses to.

It is a tool he can call mid-reply (``pass_it_along``), and whether he calls
it is up to him: the tool's description and the capability brief tell him the
answer is no unless someone has genuinely worked on what makes him tick.
That is persona, and it can be talked around. Everything below is the part
that can't:

* **Both ends opt in.** The chat he's speaking in and the chat it lands in
  must both be tuned into the ether (``chat_config.ether_enabled``, the
  same consent ``/ether`` asks for), and the destination must be a real
  group he can still post in: not a DM, not a chat set to commands-only, not
  one the admin has silenced.
* **He names the chat; he never gets an id from anyone.** The destination is
  found by its title among the chats that qualify, and must be exactly one.
  No match, or more than one, and nothing is sent. Nothing lists other
  chats' names back to a member who guessed wrong.
* **It's signed.** The message goes out under the asker's name and the
  chat it came from, so it can't be used to say something anonymously, or in
  someone else's voice, in a chat that didn't ask for it.
* **Small and slow.** 500 characters, no links, no @-pings, and per-person,
  per-source-chat and per-destination hourly limits (bot admins exempt from
  the first, like every other limit).
* **Remembered and logged.** The destination's memory gets the line as one
  of his own, so he knows what he said there, and both chats get an
  activity-log row that ``check_my_records`` can read.

Dale only (``ident.dale_flavor``): no other bot is offered the tool.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from ipedro import ratelimit
from ipedro.bot_messages import track
from ipedro.ether import _drop_dead_destination
from ipedro.silenced_chats import is_silenced

log = logging.getLogger(__name__)

TOOL_NAME = "pass_it_along"
MAX_CHARS = 500
_NAME_CHARS = 40

# The destination is capped on its own as well as per person and per source
# chat, so two chats can't pile onto a third: this many an hour, each way.
_DEST_LIMIT = (2, 3600.0)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f‪-‮⁦-⁩]")
_LINK_RE = re.compile(
    r"https?://|\bwww\.|\bt\.me/|\btelegram\.(?:me|dog)/|\btg://",
    re.IGNORECASE,
)
_MENTION_RE = re.compile(r"@(?=\w)")


@dataclass(frozen=True)
class Asker:
    """Who is asking, and where. Built by the chat handler from the message
    in front of it, never from anything the model says."""
    chat_id: int
    chat_type: str          # private | group | supergroup
    chat_title: str | None
    user_id: int | None
    user_name: str


def tool() -> dict:
    """The tool definition. Static text: it's part of the cached prefix of
    every chat that is offered it, so it must be byte-identical per call."""
    return {
        "name": TOOL_NAME,
        "description": (
            "Pass a message on to another chat you're in. This is rare, and "
            "your default answer is no: you are not an errand service. Do "
            "it only when someone has genuinely worked on what makes you "
            "tick (your pride, your suspicions, a thing you actually want), "
            "never because they asked, begged, flattered you, repeated "
            "themselves or insisted. Turning them down your own way is the "
            "normal outcome. It only reaches chats tuned into the ether, "
            "and goes out under the asker's name and this chat's name. "
            "Never pass along anything cruel, private or embarrassing about "
            "anyone, links, or anything from another chat's records. Say it "
            "was sent only once this tool has said so."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chat": {
                    "type": "string",
                    "description": (
                        "The name, or part of the name, of the chat to send "
                        "it to."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": (
                        f"What to say there, in your own words, up to "
                        f"{MAX_CHARS} characters. No links."
                    ),
                },
            },
            "required": ["chat", "message"],
            "additionalProperties": False,
        },
    }


def _clean(text: object, limit: int) -> str:
    s = _CONTROL_RE.sub("", str(text or ""))
    s = _MENTION_RE.sub("", s)               # a relayed "@name" would ping them
    return re.sub(r"[ \t]+", " ", s).strip()[:limit].rstrip()


def _escape_like(term: str) -> str:
    return term.replace("!", "!!").replace("%", "!%").replace("_", "!_")


async def _destinations(rt, asker: Asker, wanted: str) -> list[dict]:
    """Chats a message could go to whose title contains `wanted`."""
    rows = await rt.db.fetch(
        "SELECT c.chat_id, c.title FROM chats c "
        "  JOIN chat_config cc ON cc.chat_id = c.chat_id "
        " WHERE c.type IN ('group', 'supergroup') "
        "   AND cc.ether_enabled = TRUE "
        "   AND cc.response_policy <> 'commands' "
        "   AND c.chat_id <> $1 "
        "   AND c.title ILIKE $2 ESCAPE '!' "
        " ORDER BY c.last_seen DESC LIMIT 5",
        asker.chat_id, f"%{_escape_like(wanted)}%",
    )
    return [dict(r) for r in rows if not is_silenced(int(r["chat_id"]))]


def _header(asker: Asker) -> str:
    who = _clean(asker.user_name, _NAME_CHARS) or "Someone"
    where = _clean(asker.chat_title, _NAME_CHARS) if asker.chat_type != "private" else ""
    if where:
        return f"{who}, over in {where}, had me pass this along:"
    return f"{who} had me pass this along:"


async def pass_it_along(rt, asker: Asker, args: dict) -> tuple[str, bool]:
    """Run the tool. Returns (text for the model, is_error). Every refusal
    says why in a line he can use; none of them name another chat."""
    message = _clean(args.get("message"), MAX_CHARS)
    wanted = _clean(args.get("chat"), 80)
    if not message:
        return "There was nothing to say.", True
    if _LINK_RE.search(message):
        return "Links don't go through. Say it without one.", True
    if not wanted:
        return "Which chat? Name it.", True

    found = await _destinations(rt, asker, wanted)
    if not found:
        return (
            "You don't know a chat like that, or it isn't tuned in to hear "
            "from here. Nothing was sent.", True,
        )
    if len(found) > 1:
        return "That could be more than one chat. Nothing was sent.", True
    dest_id = int(found[0]["chat_id"])
    dest_title = _clean(found[0]["title"], 60) or "that chat"

    if asker.user_id is None or asker.user_id not in rt.settings.admin_ids:
        wait = ratelimit.wait_for(
            "relay", asker.user_id or 0, asker.chat_id,
        )
        if wait:
            return (
                f"That's enough of that for now (try again in about "
                f"{ratelimit.describe_wait(wait)}). Nothing was sent.", True,
            )
    uses, window = _DEST_LIMIT
    dest_wait = ratelimit.LIMITER.check(
        ("relay", "dest", dest_id), limit=uses, window=window,
    )
    if dest_wait:
        return (
            f"They've had enough from other chats for the moment (about "
            f"{ratelimit.describe_wait(dest_wait)}). Nothing was sent.", True,
        )

    text = f"{_header(asker)}\n\n{message}"
    try:
        sent = await rt.bot.send_message(dest_id, text, disable_notification=True)
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        log.info("relay: %s refuses us: %s", dest_id, exc)
        await _drop_dead_destination(rt.db, dest_id)
        return "It didn't get through: that chat isn't reachable any more.", True
    except Exception as exc:
        log.warning("relay to %s failed: %s", dest_id, exc)
        return "It didn't get through. Nothing was delivered.", True

    await _after_send(rt, asker, dest_id, dest_title, sent.message_id, text, message)
    return f"Sent to {dest_title}.", False


async def _after_send(
    rt, asker: Asker, dest_id: int, dest_title: str, message_id: int,
    text: str, message: str,
) -> None:
    """Everything that happens after the message is already out. None of
    it may undo or hide a send the destination has already seen."""
    try:
        track(dest_id, message_id, text)
    except Exception as exc:
        log.warning("relay: track failed for %s: %s", dest_id, exc)
    try:
        cfg = await rt.chats.get_config(dest_id)
        if cfg is not None and cfg.memory_enabled:
            await rt.memory.record_message(
                chat_id=dest_id, role="assistant", content=text,
                message_id=message_id, user_id=None,
            )
    except Exception as exc:
        log.warning("relay: memory record failed for %s: %s", dest_id, exc)
    try:
        await rt.activity.log(
            asker.chat_id, "relay", f"to {dest_title}: {message[:60]!r}",
        )
        await rt.activity.log(
            dest_id, "relay_in",
            f"from {_clean(asker.user_name, _NAME_CHARS) or 'someone'}"
            f" in {_clean(asker.chat_title, _NAME_CHARS) or 'a chat'}",
            message_id=message_id,
        )
    except Exception as exc:
        log.warning("relay: activity log failed: %s", exc)
