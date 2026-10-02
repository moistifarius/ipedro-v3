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

log = logging.getLogger(__name__)

CHANNEL = "bot_posts"
MAX_DEPTH = 3
MIN_GAP_SECONDS = 20.0
_KEEPALIVE_SECONDS = 30.0
_PRUNE_EVERY_SECONDS = 3600.0
_TEXT_MAX = 4096


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
            import asyncpg
            connect = asyncpg.connect
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
            await conn.add_listener(
                CHANNEL, lambda _c, _pid, _ch, payload: queue.put_nowait(payload),
            )
            backoff = 1.0
            idle = 0.0
            try:
                while not stop.is_set():
                    if prune and time.monotonic() - last_prune >= _PRUNE_EVERY_SECONDS:
                        last_prune = time.monotonic()
                        try:
                            await self.prune()
                        except Exception as exc:
                            log.info("hub: prune failed: %s", exc)
                    try:
                        payload = await asyncio.wait_for(queue.get(), 5.0)
                    except asyncio.TimeoutError:
                        idle += 5.0
                        if idle >= _KEEPALIVE_SECONDS:
                            idle = 0.0
                            await conn.execute("SELECT 1")   # raises if it's gone
                        continue
                    idle = 0.0
                    await self._deliver(payload, on_post)
            except Exception as exc:
                log.info("hub: listener dropped (%s); reconnecting", exc)
            finally:
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

    cfg = await rt.chats.get_config(post.chat_id)
    if cfg is None:
        return                      # not a chat this bot has ever been in
    await rt.users.upsert_user(
        post.bot_id, post.bot_username or None, post.bot_name, None, True,
    )
    if cfg.memory_enabled:
        await rt.memory.record_message(
            chat_id=post.chat_id, role="user", content=post.text,
            message_id=post.message_id, user_id=post.bot_id,
        )

    if (
        post.depth >= MAX_DEPTH
        or cfg.response_policy == "commands"
        or not _addressed(post, hub, ident)
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
    )
    reply = await rt.openai.chat(ctx.messages, max_tokens=300, chat_id=post.chat_id)
    if not reply or not reply.strip():
        return
    from aiogram.types import ReplyParameters
    sent = await rt.bot.send_message(
        post.chat_id, reply,
        reply_parameters=ReplyParameters(
            message_id=post.message_id, allow_sending_without_reply=True,
        ),
        disable_notification=True,
    )
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
    except Exception:
        pass


async def run(rt, settings, stop: asyncio.Event) -> None:
    """Join the hub for the life of the process."""
    from ipedro import identity
    from ipedro.db.pool import Database

    dsn = settings.hub_database_url or settings.database_url
    try:
        db = await Database.connect(dsn, min_size=1, max_size=2)
        me = await rt.bot.me()
    except Exception as exc:
        log.warning("hub: not joining (%s); bots won't hear this one", exc)
        return
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
