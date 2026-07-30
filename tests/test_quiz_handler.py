"""Flow tests for the /disgusttest handler.

Fakes the sessions/results tables in memory (reproducing the atomic
cardinality-guarded append) so the interactive contract is exercised without
Postgres: start → answer → advance, ownership gating, stale/expired taps,
cancel, finalize (image + text-fallback), result storage, and the leaderboard.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import disgust_test as dt
from ipedro.handlers.quiz import build_router


class _QuizFakeDB:
    def __init__(self) -> None:
        self.sessions: dict[tuple[int, int], dict] = {}
        self.results: dict[tuple[int, int], tuple] = {}

    async def execute(self, query, *args):
        if "INSERT INTO disgust_test_sessions" in query:
            chat_id, user_id, message_id = args
            self.sessions[(chat_id, user_id)] = {"answers": [], "message_id": message_id}
            return "INSERT 0 1"
        if "DELETE FROM disgust_test_sessions" in query:
            chat_id, user_id = args
            self.sessions.pop((chat_id, user_id), None)
            return "DELETE 1"
        if "INSERT INTO disgust_test_results" in query:
            self.results[(args[0], args[1])] = args
            return "INSERT 0 1"
        return "OK"

    async def fetchrow(self, query, *args):
        if "UPDATE disgust_test_sessions" in query and "array_append" in query:
            value, chat_id, user_id, idx = args
            sess = self.sessions.get((chat_id, user_id))
            if sess is None or len(sess["answers"]) != idx:   # cardinality guard
                return None
            sess["answers"].append(value)
            return {"answers": list(sess["answers"])}
        return None

    async def fetchval(self, query, *args):
        if "SELECT 1 FROM disgust_test_sessions" in query:
            return 1 if (args[0], args[1]) in self.sessions else None
        return None

    async def fetch(self, query, *args):
        if "FROM disgust_test_results" in query:
            (chat_id,) = args
            rows = [
                {"display_name": v[2], "food_score": v[3],
                 "general_score": v[4], "overall_score": v[8]}
                for k, v in self.results.items() if k[0] == chat_id
            ]
            rows.sort(key=lambda r: -r["overall_score"])
            return rows[:15]
        return []


def _make_rt(db, *, image=b"PNG", verdict="you are gross, congrats"):
    cfg = SimpleNamespace()
    chats = SimpleNamespace(
        upsert_chat=AsyncMock(),
        get_config=AsyncMock(return_value=cfg),
        upsert_default_config=AsyncMock(return_value=cfg),
    )
    users = SimpleNamespace(upsert_user=AsyncMock())
    openai = SimpleNamespace(
        chat=AsyncMock(return_value=verdict),
        generate_image=AsyncMock(return_value=image),
    )
    return SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset()),
        db=db, chats=chats, users=users, openai=openai, bot=SimpleNamespace(),
    )


def _handler(rt, name):
    router = build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == name)


def _cb_handler(rt, name):
    router = build_router(rt)
    return next(h.callback for h in router.observers["callback_query"].handlers
                if h.callback.__name__ == name)


def _user(uid, name="Matt"):
    return SimpleNamespace(id=uid, is_bot=False, username=name.lower(),
                           first_name=name, last_name=None)


def _msg(chat_id, uid, text):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="group", title="t"),
        from_user=_user(uid), text=text, caption=None, message_id=1,
        answer=AsyncMock(return_value=SimpleNamespace(message_id=555)),
        reply=AsyncMock(),
    )


def _cb(chat_id, uid, data):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="group"),
        edit_text=AsyncMock(), answer=AsyncMock(), answer_photo=AsyncMock(),
    )
    return SimpleNamespace(
        data=data, from_user=_user(uid), message=message, answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_start_creates_session_and_shows_first_question():
    db = _QuizFakeDB()
    rt = _make_rt(db)
    start = _handler(rt, "start_test")
    msg = _msg(100, 7, "/disgusttest")
    await start(msg)

    assert (100, 7) in db.sessions
    assert db.sessions[(100, 7)]["answers"] == []
    assert db.sessions[(100, 7)]["message_id"] == 555
    body = msg.answer.await_args.args[0]
    assert "1/16" in body


@pytest.mark.asyncio
async def test_answer_advances_to_next_question():
    db = _QuizFakeDB()
    db.sessions[(100, 7)] = {"answers": [], "message_id": 555}
    rt = _make_rt(db)
    on_answer = _cb_handler(rt, "on_answer")

    await on_answer(_cb(100, 7, "dq:7:0:4"))
    assert db.sessions[(100, 7)]["answers"] == [4]
    # message edited to question 2/16
    # (edit happens for a non-final answer)


@pytest.mark.asyncio
async def test_only_owner_can_answer():
    db = _QuizFakeDB()
    db.sessions[(100, 7)] = {"answers": [], "message_id": 555}
    rt = _make_rt(db)
    on_answer = _cb_handler(rt, "on_answer")

    cb = _cb(100, 99, "dq:7:0:4")          # user 99 taps user 7's test
    await on_answer(cb)
    assert db.sessions[(100, 7)]["answers"] == []      # untouched
    assert "not your test" in cb.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_stale_tap_is_ignored_quietly():
    db = _QuizFakeDB()
    db.sessions[(100, 7)] = {"answers": [3, 3], "message_id": 555}  # on Q3 (idx2)
    rt = _make_rt(db)
    on_answer = _cb_handler(rt, "on_answer")

    cb = _cb(100, 7, "dq:7:0:5")           # tapping Q1's button again
    await on_answer(cb)
    assert db.sessions[(100, 7)]["answers"] == [3, 3]   # no double-count
    cb.answer.assert_awaited()             # answered, but with no alert text
    assert cb.answer.await_args.args == ()


@pytest.mark.asyncio
async def test_expired_session_tells_user_to_restart():
    db = _QuizFakeDB()                      # no session at all
    rt = _make_rt(db)
    on_answer = _cb_handler(rt, "on_answer")

    cb = _cb(100, 7, "dq:7:0:5")
    await on_answer(cb)
    assert "expired" in cb.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_cancel_clears_session():
    db = _QuizFakeDB()
    db.sessions[(100, 7)] = {"answers": [1, 2], "message_id": 555}
    rt = _make_rt(db)
    on_answer = _cb_handler(rt, "on_answer")

    cb = _cb(100, 7, "dq:7:x")
    await on_answer(cb)
    assert (100, 7) not in db.sessions
    assert "cancelled" in cb.message.edit_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_final_answer_produces_image_result_and_stores_it():
    db = _QuizFakeDB()
    db.sessions[(100, 7)] = {"answers": [3] * 15, "message_id": 555}
    rt = _make_rt(db, image=b"PNGDATA")
    on_answer = _cb_handler(rt, "on_answer")

    cb = _cb(100, 7, "dq:7:15:3")          # the 16th answer
    await on_answer(cb)

    # Result photo sent with a scored caption.
    assert cb.message.answer_photo.await_count == 1
    caption = cb.message.answer_photo.await_args.kwargs["caption"]
    assert "Disgust Profile" in caption and "/6" in caption
    # Persona verdict was requested and image generated.
    rt.openai.chat.assert_awaited()
    rt.openai.generate_image.assert_awaited()
    # Stored + session cleared.
    assert (100, 7) in db.results
    assert (100, 7) not in db.sessions


@pytest.mark.asyncio
async def test_final_answer_falls_back_to_text_when_image_fails():
    db = _QuizFakeDB()
    db.sessions[(100, 7)] = {"answers": [3] * 15, "message_id": 555}
    rt = _make_rt(db, image=None)          # generation returns nothing
    on_answer = _cb_handler(rt, "on_answer")

    cb = _cb(100, 7, "dq:7:15:3")
    await on_answer(cb)

    cb.message.answer_photo.assert_not_awaited()
    # The full result was delivered as the edited message text instead.
    final_text = cb.message.edit_text.await_args.args[0]
    assert "Disgust Profile" in final_text and "For fun, not a diagnosis." in final_text
    assert (100, 7) in db.results


@pytest.mark.asyncio
async def test_verdict_fallback_when_ai_returns_none():
    db = _QuizFakeDB()
    db.sessions[(100, 7)] = {"answers": [3] * 15, "message_id": 555}
    rt = _make_rt(db, image=None, verdict=None)   # AI verdict unavailable
    on_answer = _cb_handler(rt, "on_answer")

    cb = _cb(100, 7, "dq:7:15:3")
    await on_answer(cb)
    final_text = cb.message.edit_text.await_args.args[0]
    # Deterministic fallback verdict mentions the taker by name.
    assert "Matt" in final_text


@pytest.mark.asyncio
async def test_leaderboard_empty_and_ranked():
    db = _QuizFakeDB()
    rt = _make_rt(db)
    board = _handler(rt, "leaderboard")

    empty_msg = _msg(100, 7, "/disgustboard")
    await board(empty_msg)
    assert "Nobody" in empty_msg.reply.await_args.args[0]

    # Two stored results in chat 100, one elsewhere.
    db.results[(100, 1)] = (100, 1, "Luke", 5.0, 5.0, 5, 5, 5, 5.0, "fish")
    db.results[(100, 2)] = (100, 2, "Matt", 2.0, 2.0, 2, 2, 2, 2.0, "mould")
    db.results[(200, 3)] = (200, 3, "Other", 6.0, 6.0, 6, 6, 6, 6.0, "fish")

    ranked_msg = _msg(100, 7, "/disgustboard")
    await board(ranked_msg)
    body = ranked_msg.reply.await_args.args[0]
    assert body.index("Luke") < body.index("Matt")     # 5.0 above 2.0
    assert "Other" not in body                          # chat-scoped
