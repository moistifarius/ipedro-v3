"""Karma: native-reaction scoring, self-karma and anonymous-reaction guards.

No test file existed for this handler before this one.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers.karma import build_router


def _handler(rt):
    router = build_router(rt)
    return next(h.callback for h in router.observers["message_reaction"].handlers
                if h.callback.__name__ == "on_reaction")


def _rt(*, author_row=None):
    return SimpleNamespace(
        db=SimpleNamespace(
            fetchrow=AsyncMock(return_value=author_row),
            execute=AsyncMock(),
        ),
    )


def _emoji(e):
    return SimpleNamespace(emoji=e, custom_emoji_id=None)


def _event(*, user_id=99, old=(), new=(), message_id=100, chat_id=42):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        message_id=message_id,
        user=SimpleNamespace(id=user_id) if user_id is not None else None,
        actor_chat=None,
        old_reaction=[_emoji(e) for e in old],
        new_reaction=[_emoji(e) for e in new],
    )


AUTHOR = {"user_id": 7, "first_name": "Matt", "username": "mattd"}


@pytest.mark.asyncio
async def test_a_thumbs_up_grants_one_point_to_the_author():
    rt = _rt(author_row=AUTHOR)
    await _handler(rt)(_event(user_id=99, new=("👍",)))
    args = rt.db.execute.await_args.args
    assert args[1] == 42 and args[2] == 7 and args[4] == 1


@pytest.mark.asyncio
async def test_a_thumbs_down_docks_one_point():
    rt = _rt(author_row=AUTHOR)
    await _handler(rt)(_event(user_id=99, new=("👎",)))
    assert rt.db.execute.await_args.args[4] == -1


@pytest.mark.asyncio
async def test_removing_a_reaction_is_the_negative_delta():
    rt = _rt(author_row=AUTHOR)
    await _handler(rt)(_event(user_id=99, old=("👍",), new=()))
    assert rt.db.execute.await_args.args[4] == -1


@pytest.mark.asyncio
async def test_a_net_zero_change_never_touches_the_db():
    rt = _rt(author_row=AUTHOR)
    await _handler(rt)(_event(user_id=99, old=("👍",), new=("👍",)))
    rt.db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_karma_is_blocked():
    rt = _rt(author_row=AUTHOR)
    await _handler(rt)(_event(user_id=7, new=("👍",)))   # reactor == author
    rt.db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_anonymous_reaction_is_not_eligible():
    """Telegram exposes anonymous (actor_chat) reactions with user=None —
    there's no way to tell it isn't the author farming their own message
    through their anonymous-admin identity, so it must never score."""
    rt = _rt(author_row=AUTHOR)
    await _handler(rt)(_event(user_id=None, new=("👍",)))
    rt.db.fetchrow.assert_not_awaited()      # doesn't even look up the author
    rt.db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_bot_message_with_no_author_earns_no_karma():
    rt = _rt(author_row=None)     # no row: messages table has no author for bot sends
    await _handler(rt)(_event(user_id=99, new=("👍",)))
    rt.db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unrecognized_emoji_scores_zero():
    rt = _rt(author_row=AUTHOR)
    await _handler(rt)(_event(user_id=99, new=("🤷",)))
    rt.db.execute.assert_not_awaited()
