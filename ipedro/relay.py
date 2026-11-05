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
  someone else's voice, in a chat that didn't ask for it. Both come from the
  Telegram message in front of him, never from the model, and are scrubbed
  like the message is.
* **Small and slow.** 500 characters (longer is refused, never cut), no
  links, no @-pings, no /commands, and per-person, per-source-chat and
  per-destination hourly limits (bot admins are exempt from the first only).
  A refused or definitively failed attempt gives its slots back; one that
  might have been delivered keeps them.
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
from ipedro.silenced_chats import is_silenced, list_silenced

log = logging.getLogger(__name__)

TOOL_NAME = "pass_it_along"
# The two kinds of row it writes to the activity log, and what
# introspection offers check_my_records when this tool is offered.
EVENT_SENT = "relay"
EVENT_RECEIVED = "relay_in"
MAX_CHARS = 500
_NAME_CHARS = 40

# The destination is capped on its own as well as per person and per source
# chat, so two chats can't pile onto a third: this many an hour.
_DEST_LIMIT = (2, 3600.0)

# Telegram shows posts from an anonymous admin or "send as channel" as these
# bots. They are not a person, so there's no one to sign for or to limit.
ANONYMOUS_SENDERS = frozenset({1087968824, 136817688})

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f‪-‮⁦-⁩]")

# Anything Telegram would turn into a link: a scheme, www., t.me, and a bare
# domain on a TLD that is a domain and not a word, or on a short one (bit.ly,
# youtu.be, amzn.to) when a path follows.
_TLD = (
    "com|net|org|io|gg|app|dev|xyz|info|biz|tv|uk|ca|de|ru|cn|ai|sh|link|"
    "click|site|online|store|shop|top|tk|ml|ga|cf|gq|ink|page|live|club|vip|"
    "fun|lol|pro|cloud|fm|gl|fr|nl|au|jp|kr|br|mx|es|se|pl|cz|ch|gov|edu|"
    "name|mobi|today|world|news|work|space|tech|website|win|bid|stream|"
    "download|review|party|trade|date|loan|men|icu|buzz|rest|gdn|onion"
)
_TLD_WITH_PATH = "be|to|is|it|in|no|at|me|us|co|cc|ws|ly|one|dk|fi"
_LINK_RE = re.compile(
    r"[a-z][a-z0-9+.\-]*://"
    r"|\b(?:tg|ton|ftp|magnet|mailto|sms|tel):"
    r"|\bwww\."
    r"|\b(?:t|telegram)\.(?:me|dog|org)\b"
    rf"|\b[a-z0-9][a-z0-9-]*\.(?:{_TLD})\b"
    rf"|\b[a-z0-9][a-z0-9-]*\.(?:{_TLD_WITH_PATH})/",
    re.IGNORECASE,
)
_MENTION_RE = re.compile(r"@\w")
# "/ether" in the middle of a line is tappable. Not "and/or" or "1/2" (a word
# character before the slash) and not the "/s" tone tag (one letter).
_COMMAND_RE = re.compile(r"(?<![\w/])/[a-z_]{2,}", re.IGNORECASE)

# BadRequests that mean "that chat is gone", as opposed to "that send was
# refused" (a closed forum topic, a restriction): only the first should take
# a chat off the ether.
_GONE = ("chat not found", "kicked", "not a member", "deactivated",
         "chat_write_forbidden")


@dataclass(frozen=True)
class Asker:
    """Who is asking, and where. Built by the chat handler from the message
    in front of it, never from anything the model says."""
    chat_id: int
    chat_type: str          # private | group | supergroup
    chat_title: str | None
    user_id: int
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
                        f"{MAX_CHARS} characters (longer is refused). No "
                        f"links, @-mentions or /commands."
                    ),
                },
            },
            "required": ["chat", "message"],
            "additionalProperties": False,
        },
    }


def _strip_controls(text: object) -> str:
    return _CONTROL_RE.sub("", str(text or ""))


def _name(text: object, limit: int = _NAME_CHARS) -> str:
    """A display name or chat title made safe to print in the signature:
    no control or direction characters, no '@' at all, no leading '/', and
    nothing at all if it reads as a link."""
    s = re.sub(r"\s+", " ", _strip_controls(text).replace("@", "")).strip()
    s = s.lstrip("/ ")[:limit].rstrip()
    return "" if _LINK_RE.search(s) else s


def _escape_like(term: str) -> str:
    return term.replace("!", "!!").replace("%", "!%").replace("_", "!_")


async def _destinations(rt, asker: Asker, wanted: str) -> list[dict]:
    """Chats a message could go to whose title contains `wanted`. Silenced
    chats are excluded in the query, so they can't use up its limit."""
    rows = await rt.db.fetch(
        "SELECT c.chat_id, c.title FROM chats c "
        "  JOIN chat_config cc ON cc.chat_id = c.chat_id "
        " WHERE c.type IN ('group', 'supergroup') "
        "   AND cc.ether_enabled = TRUE "
        "   AND cc.response_policy <> 'commands' "
        "   AND c.chat_id <> $1 "
        "   AND c.chat_id <> ALL($3::bigint[]) "
        "   AND c.title ILIKE $2 ESCAPE '!' "
        " ORDER BY c.last_seen DESC LIMIT 5",
        asker.chat_id, f"%{_escape_like(wanted)}%", list_silenced(),
    )
    found = [dict(r) for r in rows if not is_silenced(int(r["chat_id"]))]
    exact = [r for r in found if (r["title"] or "").strip().lower() == wanted.lower()]
    return exact if len(exact) == 1 else found


def _header(asker: Asker) -> str:
    who = _name(asker.user_name) or "Someone"
    where = _name(asker.chat_title) if asker.chat_type != "private" else ""
    if where:
        return f"{who}, over in {where}, had me pass this along:"
    return f"{who} had me pass this along:"


def _refusal(message: str) -> str | None:
    """Why this message can't go, in a line he can act on; None if it can."""
    if not message:
        return "There was nothing to say."
    if len(message) > MAX_CHARS:
        return (
            f"That's too long ({len(message)} characters; the most is "
            f"{MAX_CHARS}). Say it shorter. Nothing was sent."
        )
    if _LINK_RE.search(message):
        return "Links don't go through. Say it without one. Nothing was sent."
    if _MENTION_RE.search(message):
        return "@-mentions don't go through. Nothing was sent."
    if _COMMAND_RE.search(message):
        return "/commands don't go through. Nothing was sent."
    return None


class _Slots:
    """The hourly slots one attempt has taken, so a refused or definitively
    failed attempt can hand them back (the same courtesy ratelimit.wait_for
    extends to a refusal by the chat limit)."""

    def __init__(self) -> None:
        self._keys: list[tuple] = []

    def take(self, key: tuple, limit: tuple[int, float]) -> float:
        uses, window = limit
        wait = ratelimit.LIMITER.check(key, limit=uses, window=window)
        if not wait:
            self._keys.append(key)
        return wait

    def give_back(self) -> None:
        for key in self._keys:
            ratelimit.LIMITER.refund(key)
        self._keys.clear()


def _is_gone(exc: Exception) -> bool:
    if isinstance(exc, TelegramForbiddenError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _GONE)


async def pass_it_along(rt, asker: Asker, args: dict) -> tuple[str, bool]:
    """Run the tool. Returns (text for the model, is_error). Every refusal
    says why in a line he can use; none of them name another chat."""
    message = _strip_controls(args.get("message")).strip()
    wanted = re.sub(r"\s+", " ", _strip_controls(args.get("chat"))).strip()[:80]
    refusal = _refusal(re.sub(r"[ \t]+", " ", message))
    if refusal:
        return refusal, True
    message = re.sub(r"[ \t]+", " ", message)
    if not wanted:
        return "Which chat? Name it.", True
    if not asker.user_id or asker.user_id in ANONYMOUS_SENDERS:
        return "There's no telling who is asking. Nothing was sent.", True

    found = await _destinations(rt, asker, wanted)
    if not found:
        return (
            "You don't know a chat like that, or it isn't tuned in to hear "
            "from here. Nothing was sent.", True,
        )
    if len(found) > 1:
        return "That could be more than one chat. Nothing was sent.", True
    dest_id = int(found[0]["chat_id"])
    dest_title = _name(found[0]["title"], 60) or "that chat"

    slots = _Slots()
    waits = []
    if asker.user_id not in rt.settings.admin_ids:
        waits.append(slots.take(("relay", "user", asker.user_id),
                                ratelimit.LIMITS["relay"]))
    waits.append(slots.take(("relay", "chat", asker.chat_id),
                            ratelimit.CHAT_LIMITS["relay"]))
    waits.append(slots.take(("relay", "dest", dest_id), _DEST_LIMIT))
    wait = max(waits)
    if wait:
        slots.give_back()
        return (
            f"That's enough of that for now (try again in about "
            f"{ratelimit.describe_wait(wait)}). Nothing was sent.", True,
        )

    text = f"{_header(asker)}\n\n{message}"
    try:
        sent = await rt.bot.send_message(dest_id, text, disable_notification=True)
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        log.info("relay: %s refused the message: %s", dest_id, exc)
        slots.give_back()               # Telegram said no: it wasn't delivered
        if _is_gone(exc):
            await _drop_dead_destination(rt.db, dest_id)
            return "It didn't get through: that chat isn't reachable any more.", True
        return "It didn't get through: Telegram refused it. Nothing was sent.", True
    except Exception as exc:
        log.warning("relay to %s failed: %s", dest_id, exc)
        # Could have been delivered (a timeout), so the slots stay spent.
        return (
            "It may not have gone through: the send failed or timed out. "
            "Don't say it was sent.", True,
        )

    await _after_send(rt, asker, dest_id, dest_title, sent.message_id, text, message)
    return f"Sent to {dest_title}.", False


async def _after_send(
    rt, asker: Asker, dest_id: int, dest_title: str, message_id: int,
    text: str, message: str,
) -> None:
    """Everything that happens after the message is already out. None of
    it may undo or hide a send the destination has already seen, so each
    step stands alone."""
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
            asker.chat_id, EVENT_SENT, f"to {dest_title}: {message[:200]!r}",
        )
    except Exception as exc:
        log.warning("relay: activity log failed: %s", exc)
    try:
        await rt.activity.log(
            dest_id, EVENT_RECEIVED,
            f"from {_name(asker.user_name) or 'someone'}"
            f" in {_name(asker.chat_title) or 'a chat'}",
            message_id=message_id,
        )
    except Exception as exc:
        log.warning("relay: activity log failed: %s", exc)
