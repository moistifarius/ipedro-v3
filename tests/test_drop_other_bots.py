"""Another bot's messages never reach the handlers.

Telegram delivered a persona bot's group replies to Dale (and Dale's to it)
as ordinary updates. The chat path answered each as if a person had replied,
with none of the hub's loop rules, and the two answered each other every few
seconds until one was stopped (2026-11-12). Bots hear each other through the
hub only; a placeholder bot sender for an anonymous admin or a channel still
gets through.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Chat, Message, Update, User

from ipedro import bot as bot_mod

GROUP = Chat(id=-1002568291286, type="supergroup", title="the chat")
PERSON = User(id=315660812, is_bot=False, first_name="Matt")
OTHER_BOT = User(id=8644202972, is_bot=True, first_name="Michaels Waifu")
# What Telegram puts in from_user for an anonymous admin / a channel.
ANON_ADMIN = User(id=1087968824, is_bot=True, first_name="Group",
                  username="GroupAnonymousBot")
CHANNEL_BOT = User(id=136817688, is_bot=True, first_name="Channel",
                   username="Channel_Bot")
A_CHANNEL = Chat(id=-1001111111111, type="channel", title="a channel")


def _message(sender, *, sender_chat=None, mid=1):
    return Message(
        message_id=mid, date=dt.datetime.now(dt.timezone.utc), chat=GROUP,
        from_user=sender, sender_chat=sender_chat, text="Dale, buddy",
    )


@pytest.mark.parametrize(
    ("message", "dropped"),
    [
        (_message(OTHER_BOT), True),
        (_message(PERSON), False),
        (_message(ANON_ADMIN, sender_chat=GROUP), False),
        (_message(CHANNEL_BOT, sender_chat=A_CHANNEL), False),
        (Message(message_id=1, date=dt.datetime.now(dt.timezone.utc),
                 chat=GROUP, text="no sender"), False),
    ],
    ids=["other-bot", "person", "anonymous-admin", "channel", "no-sender"],
)
def test_from_another_bot(message, dropped):
    assert bot_mod.from_another_bot(message) is dropped


def _dispatcher_hearing(seen: list) -> Dispatcher:
    dp = Dispatcher()
    dp.message.outer_middleware(bot_mod._drop_other_bots)
    dp.edited_message.outer_middleware(bot_mod._drop_other_bots)
    router = Router()

    @router.message()
    async def heard(msg: Message) -> None:
        seen.append(("message", msg.from_user.id))

    @router.edited_message()
    async def heard_edit(msg: Message) -> None:
        seen.append(("edit", msg.from_user.id))

    dp.include_router(router)
    return dp


async def test_another_bots_message_never_reaches_a_handler():
    seen: list = []
    dp = _dispatcher_hearing(seen)
    bot = Bot("123456:TEST")
    try:
        await dp.feed_update(bot, Update(update_id=1, message=_message(OTHER_BOT)))
        await dp.feed_update(
            bot, Update(update_id=2, edited_message=_message(OTHER_BOT, mid=2)),
        )
        await dp.feed_update(bot, Update(update_id=3, message=_message(PERSON, mid=3)))
        await dp.feed_update(
            bot, Update(update_id=4,
                        message=_message(ANON_ADMIN, sender_chat=GROUP, mid=4)),
        )
    finally:
        await bot.session.close()
    assert seen == [("message", PERSON.id), ("message", ANON_ADMIN.id)]


def test_the_real_dispatcher_drops_them_before_every_router():
    dp = bot_mod.build_dispatcher(MagicMock())
    assert bot_mod._drop_other_bots in list(dp.message.outer_middleware)
    assert bot_mod._drop_other_bots in list(dp.edited_message.outer_middleware)
