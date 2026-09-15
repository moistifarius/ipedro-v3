"""Tests for /quote, /quotes, /unquote.

These pin the behaviour that was visibly broken in real use:

  * quote numbers are PER CHAT (#1, #2, #3…), not the global BIGSERIAL id
    that produced gappy #3/#17/#42 output;
  * replying /quote to the BOT's own message is refused (no more
    "📜 #2 Dale: 📜 #12 Matt: …" quote-ception);
  * replying /quote to a message with no words (sticker / un-captioned
    photo) is refused instead of silently dumping a random quote;
  * /unquote deletes by the per-chat number the user actually sees.

The DB is faked with an in-memory stand-in that reproduces the seq
allocation contract (COALESCE(MAX(seq),0)+1 per chat) so the numbering
guarantees are actually exercised, not just mocked away.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers.utility import build_router


class _QuotesFakeDB:
    """Reproduces just the quotes SQL: per-chat seq on INSERT, random pick,
    newest-first list, and scoped delete-by-seq."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._next_id = 1

    async def fetchval(self, query, *args):
        assert "INSERT INTO quotes" in query
        (chat_id, quoted_user_id, quoted_name, text,
         saved_by, source_message_id) = args
        seq = max(
            (r["seq"] for r in self.rows if r["chat_id"] == chat_id),
            default=0,
        ) + 1
        self.rows.append({
            "id": self._next_id, "chat_id": chat_id, "seq": seq,
            "quoted_user_id": quoted_user_id, "quoted_name": quoted_name,
            "text": text, "saved_by": saved_by,
            "source_message_id": source_message_id,
        })
        self._next_id += 1
        return seq

    async def fetchrow(self, query, *args):
        assert "ORDER BY random()" in query
        (chat_id,) = args
        here = [r for r in self.rows if r["chat_id"] == chat_id]
        if not here:
            return None
        r = here[0]  # deterministic stand-in for random()
        return {"seq": r["seq"], "quoted_name": r["quoted_name"],
                "text": r["text"]}

    async def fetch(self, query, *args):
        assert "ORDER BY seq DESC" in query
        (chat_id,) = args
        here = sorted(
            (r for r in self.rows if r["chat_id"] == chat_id),
            key=lambda r: -r["seq"],
        )[:20]
        return [{"seq": r["seq"], "quoted_name": r["quoted_name"],
                 "text": r["text"]} for r in here]

    async def execute(self, query, *args):
        assert "DELETE FROM quotes" in query
        seq, chat_id = args
        before = len(self.rows)
        self.rows = [
            r for r in self.rows
            if not (r["seq"] == seq and r["chat_id"] == chat_id)
        ]
        return f"DELETE {before - len(self.rows)}"


def _make_rt(db) -> SimpleNamespace:
    cfg = SimpleNamespace()
    chats = SimpleNamespace(
        upsert_chat=AsyncMock(),
        get_config=AsyncMock(return_value=cfg),
        upsert_default_config=AsyncMock(return_value=cfg),
    )
    users = SimpleNamespace(upsert_user=AsyncMock())
    settings = SimpleNamespace(admin_ids=frozenset())
    return SimpleNamespace(
        settings=settings, db=db, chats=chats, users=users,
        openai=SimpleNamespace(), bot=SimpleNamespace(),
    )


def _handler(rt, name: str):
    router = build_router(rt)
    return next(
        h.callback for h in router.observers["message"].handlers
        if h.callback.__name__ == name
    )


def _user(uid: int, name: str, *, is_bot: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=uid, is_bot=is_bot, username=name.lower(),
                           first_name=name, last_name=None)


def _msg(*, chat_id: int, text: str, sender: SimpleNamespace,
         reply_to=None) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="group", title="t"),
        from_user=sender, text=text, caption=None, message_id=99,
        reply_to_message=reply_to, reply=AsyncMock(),
    )


def _replied(text, *, from_user, caption=None, mid=1) -> SimpleNamespace:
    return SimpleNamespace(text=text, caption=caption, from_user=from_user,
                           message_id=mid)


@pytest.mark.asyncio
async def test_quote_saves_with_per_chat_seq_starting_at_one():
    db = _QuotesFakeDB()
    rt = _make_rt(db)
    quote = _handler(rt, "quote")

    matt = _user(1, "Matt")
    luke = _user(2, "Luke")

    await quote(_msg(chat_id=100, text="/quote", sender=matt,
                     reply_to=_replied("first funny thing", from_user=luke)))
    await quote(_msg(chat_id=100, text="/quote", sender=matt,
                     reply_to=_replied("second funny thing", from_user=matt)))

    seqs = [r["seq"] for r in db.rows]
    assert seqs == [1, 2]                       # contiguous, per-chat, from 1
    assert db.rows[0]["quoted_name"] == "Luke"  # attributed to the speaker


@pytest.mark.asyncio
async def test_seq_is_isolated_between_chats():
    """The core bug: global ids made one chat show #3/#17/#42. Each chat must
    start its own numbering at #1."""
    db = _QuotesFakeDB()
    rt = _make_rt(db)
    quote = _handler(rt, "quote")
    speaker = _user(2, "Luke")

    await quote(_msg(chat_id=100, text="/quote", sender=_user(1, "Matt"),
                     reply_to=_replied("chat A line", from_user=speaker)))
    await quote(_msg(chat_id=200, text="/quote", sender=_user(1, "Matt"),
                     reply_to=_replied("chat B line", from_user=speaker)))

    by_chat = {r["chat_id"]: r["seq"] for r in db.rows}
    assert by_chat == {100: 1, 200: 1}          # both chats' first quote is #1


@pytest.mark.asyncio
async def test_quote_refuses_to_save_the_bot():
    """Replying /quote to my own /quotes output must not save it back into
    itself (quote-ception)."""
    db = _QuotesFakeDB()
    rt = _make_rt(db)
    quote = _handler(rt, "quote")

    bot = _user(999, "Dale", is_bot=True)
    msg = _msg(chat_id=100, text="/quote", sender=_user(1, "Matt"),
               reply_to=_replied("📜 #12 Matt: spread your lips…", from_user=bot))
    await quote(msg)

    assert db.rows == []                         # nothing saved
    reply = msg.reply.await_args.args[0].lower()
    assert "my own" in reply or "people say" in reply


@pytest.mark.asyncio
async def test_quote_refuses_wordless_message_instead_of_random():
    """Replying /quote to a sticker/un-captioned photo used to fall through to
    random-quote mode. It must now say so and NOT touch the random path."""
    db = _QuotesFakeDB()
    # Pre-seed a quote so, if the buggy random path ran, we'd see it echoed.
    db.rows.append({"id": 1, "chat_id": 100, "seq": 1, "quoted_user_id": 2,
                    "quoted_name": "Luke", "text": "an old quote",
                    "saved_by": 1, "source_message_id": 5})
    rt = _make_rt(db)
    quote = _handler(rt, "quote")

    sticker = _replied(None, from_user=_user(2, "Luke"), caption=None)
    msg = _msg(chat_id=100, text="/quote", sender=_user(1, "Matt"),
               reply_to=sticker)
    await quote(msg)

    assert len(db.rows) == 1                     # nothing new saved
    reply = msg.reply.await_args.args[0].lower()
    assert "words" in reply
    assert "an old quote" not in reply           # did NOT dump a random quote


@pytest.mark.asyncio
async def test_quote_alone_returns_random():
    db = _QuotesFakeDB()
    db.rows.append({"id": 1, "chat_id": 100, "seq": 7, "quoted_user_id": 2,
                    "quoted_name": "Luke", "text": "you should milk it",
                    "saved_by": 1, "source_message_id": 5})
    rt = _make_rt(db)
    quote = _handler(rt, "quote")

    msg = _msg(chat_id=100, text="/quote", sender=_user(1, "Matt"),
               reply_to=None)
    await quote(msg)

    reply = msg.reply.await_args.args[0]
    assert "#7" in reply and "Luke" in reply and "milk it" in reply


@pytest.mark.asyncio
async def test_quotes_list_newest_first_by_seq():
    db = _QuotesFakeDB()
    rt = _make_rt(db)
    quote = _handler(rt, "quote")
    for i, line in enumerate(("one", "two", "three"), start=1):
        await quote(_msg(chat_id=100, text="/quote", sender=_user(1, "Matt"),
                         reply_to=_replied(line, from_user=_user(2, "Luke"),
                                           mid=i)))
    listing = _handler(rt, "quotes_list")
    msg = _msg(chat_id=100, text="/quotes", sender=_user(1, "Matt"))
    await listing(msg)

    body = msg.reply.await_args.args[0]
    # Newest (#3) first, oldest (#1) last.
    assert body.index("#3") < body.index("#2") < body.index("#1")


@pytest.mark.asyncio
async def test_unquote_deletes_by_seq_and_tolerates_hash():
    db = _QuotesFakeDB()
    rt = _make_rt(db)
    quote = _handler(rt, "quote")
    for line in ("one", "two"):
        await quote(_msg(chat_id=100, text="/quote", sender=_user(1, "Matt"),
                         reply_to=_replied(line, from_user=_user(2, "Luke"))))

    unquote = _handler(rt, "unquote")
    msg = _msg(chat_id=100, text="/unquote #1", sender=_user(1, "Matt"))
    await unquote(msg)

    assert [r["seq"] for r in db.rows] == [2]    # #1 gone, #2 stays
    assert "Deleted" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_unquote_wont_reach_into_another_chat():
    db = _QuotesFakeDB()
    rt = _make_rt(db)
    quote = _handler(rt, "quote")
    await quote(_msg(chat_id=100, text="/quote", sender=_user(1, "Matt"),
                     reply_to=_replied("A's quote", from_user=_user(2, "Luke"))))

    unquote = _handler(rt, "unquote")
    # Chat B tries to delete #1 — its own #1 doesn't exist, A's must survive.
    msg = _msg(chat_id=200, text="/unquote 1", sender=_user(1, "Matt"))
    await unquote(msg)

    assert len(db.rows) == 1                      # A's quote untouched
    assert "No quote" in msg.reply.await_args.args[0]
