"""How the bots hear each other.

Telegram never delivers one bot's messages in a group to another bot, so
two bots in the same chat are deaf to each other unless they share a
channel of their own. This is it: every bot publishes what it says in a
group to `bot_posts` in the hub database (Dale's), with a NOTIFY, and
every bot LISTENs. A bot that hears another bot's line in a chat it's in
remembers it as part of that chat's conversation, and answers only when
it was named or replied to.

Loop protection is the point of most of this file. Two bots that answer
whenever they're addressed will happily address each other forever, so:

* Every post carries a depth: 0 when the bot was answering a human (or
  speaking on its own), parent + 1 when it was answering another bot.
  Nothing answers a post at MAX_DEPTH, so one human line sets off at
  most MAX_DEPTH bot-to-bot replies, however many bots there are.
* Each bot leaves MIN_GAP_SECONDS between its own bot-triggered replies
  in a chat, so even a legal chain can't machine-gun the room.
* A human's /shutup on a bot holds here too, and a commands-only chat
  never gets a bot-triggered reply.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from ipedro.chat_policy import IncomingMessage, should_respond

log = logging.getLogger(__name__)

CHANNEL = "bot_posts"
# A listening connection mostly waits, but its keepalive ("SELECT 1") is a
# command, and with no timeout a silently dropped connection stalled that
# probe for the kernel's TCP timeout (many minutes) instead of seconds.
_LISTEN_COMMAND_TIMEOUT = 15.0
MAX_DEPTH = 3
MIN_GAP_SECONDS = 20.0
_KEEPALIVE_SECONDS = 30.0
_POLL_SECONDS = 1.0            # how often the listen loop looks up from the queue
_PRUNE_EVERY_SECONDS = 3600.0
_TEXT_MAX = 4096
_MAX_CONCURRENT_POSTS = 4      # handlers running at once; the rest queue
# Supergroups and channels have ids of the form -100<digits>, i.e. at or
# below this. Only there is a message id the same number for every member;
# in a basic group each account numbers its own messages.
_SUPERGROUP_MAX_ID = -1_000_000_000_000


def shares_message_ids(chat_id: int) -> bool:
    """Is a message id in this chat the same for every bot in it? True in
    supergroups and channels; in a basic group another bot's message_id is
    from its own sequence and can collide with ours."""
    return chat_id <= _SUPERGROUP_MAX_ID


@dataclass(frozen=True)
class Post:
    id: int
    chat_id: int
    message_id: int
    bot_id: int
    bot_username: str
    bot_name: str
    text: str
    reply_to_user_id: int | None
    depth: int

    @classmethod
    def from_record(cls, r) -> Post:
        return cls(
            id=r["id"], chat_id=r["chat_id"], message_id=r["message_id"],
            bot_id=r["bot_id"], bot_username=r["bot_username"] or "",
            bot_name=r["bot_name"], text=r["text"],
            reply_to_user_id=r["reply_to_user_id"], depth=r["depth"],
        )


async def _connect_listener(dsn: str):
    import asyncpg

    return await asyncpg.connect(dsn, command_timeout=_LISTEN_COMMAND_TIMEOUT)


class Hub:
    def __init__(self, db, *, bot_id: int, bot_username: str, bot_name: str):
        self.db = db
        self.bot_id = bot_id
        self.bot_username = bot_username
        self.bot_name = bot_name
        # chat_id -> when this bot last answered another bot there
        self._last_bot_reply: dict[int, float] = {}

    async def publish(
        self, chat_id: int, message_id: int, text: str, *,
        reply_to_user_id: int | None = None, depth: int = 0,
    ) -> None:
        await self.db.execute(
            f"""
            WITH p AS (
                INSERT INTO bot_posts (chat_id, message_id, bot_id,
                    bot_username, bot_name, text, reply_to_user_id, depth)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
            )
            SELECT pg_notify('{CHANNEL}', id::text) FROM p
            """,
            chat_id, message_id, self.bot_id, self.bot_username,
            self.bot_name, text[:_TEXT_MAX], reply_to_user_id, depth,
        )

    async def fetch(self, post_id: int) -> Post | None:
        row = await self.db.fetchrow(
            "SELECT * FROM bot_posts WHERE id = $1", post_id,
        )
        return Post.from_record(row) if row else None

    def may_answer(self, chat_id: int, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        last = self._last_bot_reply.get(chat_id)
        return last is None or now - last >= MIN_GAP_SECONDS

    def answered(self, chat_id: int, *, now: float | None = None) -> None:
        self._last_bot_reply[chat_id] = time.monotonic() if now is None else now

    async def prune(self) -> None:
        await self.db.execute(
            "DELETE FROM bot_posts WHERE created_at < NOW() - INTERVAL '1 day'"
        )

    async def listen(self, dsn: str, on_post, stop: asyncio.Event, *,
                     prune: bool = False, connect=None) -> None:
        """Deliver every other bot's post to on_post until stop. Reconnects
        with backoff; posts that land while disconnected are missed, which
        only means a bot didn't hear a line."""
        if connect is None:
            connect = _connect_listener
        backoff = 1.0
        last_prune = 0.0
        while not stop.is_set():
            try:
                conn = await connect(dsn)
            except Exception as exc:
                log.info("hub: can't connect (%s); retrying in %ss", exc, backoff)
                await _sleep(stop, backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            queue: asyncio.Queue[str] = asyncio.Queue()
            idle = 0.0
            # Handlers run as tasks, not inline: a reply is several seconds
            # of model call, and awaiting one here froze delivery of every
            # other post and the keepalive below until it finished.
            gate = asyncio.Semaphore(_MAX_CONCURRENT_POSTS)
            running: set[asyncio.Task] = set()

            async def _handle(payload: str) -> None:
                async with gate:
                    await self._deliver(payload, on_post)

            try:
                # Inside the try: a failure here (LISTEN refused, the
                # connection dropping right after connect) used to escape
                # this loop and end the hub for the life of the process,
                # leaking the connection.
                await conn.add_listener(
                    CHANNEL,
                    lambda _c, _pid, _ch, payload: queue.put_nowait(payload),
                )
                backoff = 1.0
                while not stop.is_set():
                    if prune and time.monotonic() - last_prune >= _PRUNE_EVERY_SECONDS:
                        last_prune = time.monotonic()
                        try:
                            await self.prune()
                        except Exception as exc:
                            log.info("hub: prune failed: %s", exc)
                    try:
                        payload = await asyncio.wait_for(queue.get(), _POLL_SECONDS)
                    except asyncio.TimeoutError:
                        idle += _POLL_SECONDS
                        if idle >= _KEEPALIVE_SECONDS:
                            idle = 0.0
                            await conn.execute("SELECT 1")   # raises if it's gone
                        continue
                    idle = 0.0
                    task = asyncio.create_task(_handle(payload), name="hub-post")
                    running.add(task)
                    task.add_done_callback(running.discard)
            except Exception as exc:
                log.info("hub: listener dropped (%s); reconnecting", exc)
                await _sleep(stop, backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                for task in running:
                    task.cancel()
                if running:
                    await asyncio.gather(*running, return_exceptions=True)
                try:
                    await conn.close()
                except Exception:
                    pass

    async def _deliver(self, payload: str, on_post) -> None:
        try:
            post = await self.fetch(int(payload))
        except Exception as exc:
            log.info("hub: couldn't read post %r: %s", payload, exc)
            return
        if post is None or post.bot_id == self.bot_id:
            return
        try:
            await on_post(post)
        except Exception:
            log.warning("hub: handling post %s failed", post.id, exc_info=True)


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except asyncio.TimeoutError:
        pass


# ── publishing from anywhere ─────────────────────────────────────────────────

_hub: Hub | None = None
_pending: set[asyncio.Task] = set()


def set_hub(hub: Hub | None) -> None:
    global _hub
    _hub = hub


def publish_soon(
    chat_id: int, message_id: int | None, text: str | None, *,
    reply_to_user_id: int | None = None, depth: int = 0,
) -> None:
    """Fire-and-forget publish, callable from sync code (track()). Group
    chats only — no other bot is in anyone's DM — and never raises: a
    hub hiccup must not cost the message it's describing."""
    hub = _hub
    if hub is None or message_id is None or not text or chat_id >= 0:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _go() -> None:
        try:
            await hub.publish(
                chat_id, int(message_id), text,
                reply_to_user_id=reply_to_user_id, depth=depth,
            )
        except Exception as exc:
            log.info("hub: publish failed in %s: %s", chat_id, exc)

    task = loop.create_task(_go())
    _pending.add(task)
    task.add_done_callback(_pending.discard)


# ── hearing another bot ──────────────────────────────────────────────────────

# Chats Telegram told us we can't post in (kicked, or the chat is gone), and
# until when. A chat keeps its config row after the bot is removed, so without
# this every line another bot says there that names this one would pay for a
# model call and then fail to send, for as long as the other bot stays.
_LEFT_FOR_SECONDS = 3600.0
_left_until: dict[int, float] = {}


def _has_left(chat_id: int) -> bool:
    until = _left_until.get(chat_id)
    if until is None:
        return False
    if time.monotonic() >= until:
        del _left_until[chat_id]
        return False
    return True


def _addressed(post: Post, hub: Hub, ident) -> bool:
    return (
        post.reply_to_user_id == hub.bot_id
        or ident.name_re.search(post.text) is not None
    )


async def handle_post(rt, ident, hub: Hub, post: Post) -> None:
    """Another bot said something in a group. Remember it if this bot is
    in that chat; answer it if it was meant for this bot and the loop
    rules allow."""
    from ipedro.bot_messages import track
    from ipedro.capabilities import capability_brief
    from ipedro.memory.context_builder import build_context
    from ipedro.user_flags import has_flag

    if _has_left(post.chat_id):
        return
    cfg = await rt.chats.get_config(post.chat_id)
    if cfg is None:
        return                      # not a chat this bot has ever been in
    await rt.users.upsert_user(
        post.bot_id, post.bot_username or None, post.bot_name, None, True,
    )
    shared_ids = shares_message_ids(post.chat_id)
    if cfg.memory_enabled:
        await rt.memory.record_message(
            chat_id=post.chat_id, role="user", content=post.text,
            # The other bot's id is only a valid key here when ids are
            # shared (supergroups). In a basic group it's from that bot's
            # own sequence and collides with a real message of ours, whose
            # row the dedupe then kept and whose embedding we overwrote.
            message_id=post.message_id if shared_ids else None,
            user_id=post.bot_id,
        )

    named = ident.name_re.search(post.text) is not None
    replied = post.reply_to_user_id == hub.bot_id
    if (
        post.depth >= MAX_DEPTH
        or not (named or replied)
        # The chat's own rule decides whether being named or replied to is
        # enough ('commands' never; 'reply' only a reply; and so on).
        or not should_respond(
            cfg.response_policy,
            IncomingMessage(
                text=post.text, has_mention_of_bot=named, is_reply_to_bot=replied,
                is_command=False, chat_type="supergroup",
            ),
        )
        or not hub.may_answer(post.chat_id)
        or await has_flag(rt.db, post.chat_id, post.bot_id, "shutup")
    ):
        return
    hub.answered(post.chat_id)      # claim the slot before the slow part

    ctx = await build_context(
        store=rt.memory, settings=rt.settings, chat_id=post.chat_id,
        persona=cfg.persona, persona_custom=cfg.persona_custom,
        latest_user_text=post.text, latest_user_name=post.bot_name,
        extra_system=None, memory_enabled=cfg.memory_enabled,
        persona_override=None,
        capabilities=capability_brief(cfg, dale_flavor=ident.dale_flavor),
        terse=ident.terse,
    )
    reply = await rt.openai.chat(
        ctx.messages, max_tokens=ident.hub_reply_tokens, chat_id=post.chat_id,
    )
    if not reply or not reply.strip():
        return
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
    from aiogram.types import ReplyParameters
    kwargs = {}
    if shared_ids:                  # else it would quote the wrong message
        kwargs["reply_parameters"] = ReplyParameters(
            message_id=post.message_id, allow_sending_without_reply=True,
        )
    try:
        sent = await rt.bot.send_message(
            post.chat_id, reply, disable_notification=True, **kwargs,
        )
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        # Removed from the chat, or it's gone: nothing to answer into. Stop
        # spending model calls on it for a while.
        log.info("hub: can't answer in %s: %s", post.chat_id, exc)
        if isinstance(exc, TelegramForbiddenError) or "chat not found" in str(exc).lower():
            _left_until[post.chat_id] = time.monotonic() + _LEFT_FOR_SECONDS
        return
    track(
        post.chat_id, sent.message_id, reply,
        replied_to_user_id=post.bot_id, hub_depth=post.depth + 1,
    )
    if cfg.memory_enabled:
        await rt.memory.record_message(
            chat_id=post.chat_id, role="assistant", content=reply,
            message_id=sent.message_id, user_id=None,
        )
    try:
        await rt.activity.log(
            post.chat_id, "ai_reply",
            f"answered {post.bot_name} (another bot, depth {post.depth})",
            message_id=sent.message_id,
        )
    except Exception as exc:
        log.debug("hub: couldn't log the answer in %s: %s", post.chat_id, exc)


async def run(rt, settings, stop: asyncio.Event) -> None:
    """Join the hub for the life of the process."""
    from ipedro import identity
    from ipedro.db.pool import Database

    dsn = settings.hub_database_url or settings.database_url
    # Retry rather than give up: one hiccup at startup (Postgres still
    # coming up, a slow getMe) used to leave this bot deaf and mute on the
    # hub until its next restart, with the rest of it running fine.
    backoff = 1.0
    db = me = None
    while not stop.is_set():
        try:
            # One pooled connection: this side only publishes, which is
            # rare. Every bot holds a pool, this, and the listener, against
            # one Postgres (see docs/DEPLOY.md on max_connections).
            db = await Database.connect(dsn, min_size=1, max_size=1)
            me = await rt.bot.me()
            break
        except Exception as exc:
            log.warning("hub: not joined yet (%s); retrying in %ss", exc, backoff)
            if db is not None:
                await db.close()
                db = None
            await _sleep(stop, backoff)
            backoff = min(backoff * 2, 60.0)
    if db is None or me is None:
        return                      # asked to stop before it could join
    ident = identity.from_settings(settings)
    hub = Hub(db, bot_id=me.id, bot_username=me.username or "",
              bot_name=ident.name)
    set_hub(hub)
    try:
        await hub.listen(
            dsn, lambda post: handle_post(rt, ident, hub, post), stop,
            prune=bool(getattr(settings, "manages_bots", False)),
        )
    finally:
        set_hub(None)
        await db.close()
