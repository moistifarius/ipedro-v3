"""Flow tests for the /disgusttest handler.

Fakes the sessions/results/image-cache tables in memory (reproducing the
atomic cardinality-guarded append) so the interactive contract is exercised
without Postgres: start → answer → advance in both text and picture mode,
ownership gating, stale/expired taps, cancel, finalize (image + text-fallback),
result storage, the chat percentile, retake, warm-up, and the leaderboard.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import disgust_test as dt
from ipedro.handlers import quiz as quiz_mod
from ipedro.handlers.quiz import build_router


@pytest.fixture(autouse=True)
def _no_background_warmup(monkeypatch):
    """Stop _begin from spawning a real background image warm-up task."""
    monkeypatch.setattr(quiz_mod, "_kick_warmup", lambda rt: None)


class _QuizFakeDB:
    def __init__(self) -> None:
        self.sessions: dict[tuple[int, int], dict] = {}
        self.results: dict[tuple[int, int], tuple] = {}
        self.images: dict[str, bytes] = {}

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
        if "INSERT INTO disgust_item_images" in query:
            key, png = args
            self.images.setdefault(key, png)
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
        if "COUNT(*) AS total" in query and "disgust_test_results" in query:
            chat_id, score, user_id = args
            others = [v for k, v in self.results.items()
                      if k[0] == chat_id and k[1] != user_id]
            below = sum(1 for v in others if v[8] < score)   # v[8] = overall_score
            return {"total": len(others), "below": below}
        return None

    async def fetchval(self, query, *args):
        if "SELECT 1 FROM disgust_test_sessions" in query:
            return 1 if (args[0], args[1]) in self.sessions else None
        if "COUNT(*) FROM disgust_item_images" in query:
            return len(self.images)
        if "SELECT 1 FROM disgust_item_images" in query:
            return 1 if args[0] in self.images else None
        if "SELECT png FROM disgust_item_images" in query:
            return self.images.get(args[0])
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


def _seed_all_images(db):
    for it in dt.ALL_ITEMS:
        db.images[it.key] = b"IMG"


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
        answer_photo=AsyncMock(return_value=SimpleNamespace(message_id=556)),
        reply=AsyncMock(),
    )


def _cb(chat_id, uid, data, *, photo=None):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="group"),
        photo=photo,
        edit_text=AsyncMock(), edit_caption=AsyncMock(), edit_media=AsyncMock(),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=557)),
        answer_photo=AsyncMock(return_value=SimpleNamespace(message_id=558)),
    )
    return SimpleNamespace(
        data=data, from_user=_user(uid), message=message, answer=AsyncMock(),
    )


# --------------------------------------------------------------- text flow
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
    msg.answer_photo.assert_not_awaited()          # no images cached → text mode


@pytest.mark.asyncio
async def test_answer_advances_to_next_question():
    db = _QuizFakeDB()
    db.sessions[(100, 7)] = {"answers": [], "message_id": 555}
    rt = _make_rt(db)
    on_answer = _cb_handler(rt, "on_answer")

    await on_answer(_cb(100, 7, "dq:7:0:4"))
    assert db.sessions[(100, 7)]["answers"] == [4]


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
    cb.answer.assert_awaited()
    assert cb.answer.await_args.args == ()


@pytest.mark.asyncio
async def test_expired_session_tells_user_to_restart():
    db = _QuizFakeDB()
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

    assert cb.message.answer_photo.await_count == 1
    caption = cb.message.answer_photo.await_args.kwargs["caption"]
    assert "Disgust Profile" in caption and "/6" in caption
    rt.openai.chat.assert_awaited()
    rt.openai.generate_image.assert_awaited()
    assert (100, 7) in db.results
    assert (100, 7) not in db.sessions
    # A Retake button rides along on the result.
    kb = cb.message.answer_photo.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "dqx:7"


@pytest.mark.asyncio
async def test_final_answer_falls_back_to_text_when_image_fails():
    db = _QuizFakeDB()
    db.sessions[(100, 7)] = {"answers": [3] * 15, "message_id": 555}
    rt = _make_rt(db, image=None)
    on_answer = _cb_handler(rt, "on_answer")

    cb = _cb(100, 7, "dq:7:15:3")
    await on_answer(cb)

    cb.message.answer_photo.assert_not_awaited()
    final_text = cb.message.edit_text.await_args.args[0]
    assert "Disgust Profile" in final_text and "For fun, not a diagnosis." in final_text
    assert (100, 7) in db.results


@pytest.mark.asyncio
async def test_verdict_fallback_when_ai_returns_none():
    db = _QuizFakeDB()
    db.sessions[(100, 7)] = {"answers": [3] * 15, "message_id": 555}
    rt = _make_rt(db, image=None, verdict=None)
    on_answer = _cb_handler(rt, "on_answer")

    cb = _cb(100, 7, "dq:7:15:3")
    await on_answer(cb)
    final_text = cb.message.edit_text.await_args.args[0]
    assert "Matt" in final_text


@pytest.mark.asyncio
async def test_result_shows_chat_percentile_when_others_have_taken_it():
    db = _QuizFakeDB()
    # Two other, lower-scoring takers already exist in this chat.
    db.results[(100, 1)] = (100, 1, "Luke", 1.0, 1.0, 1, 1, 1, 1.0, "fish")
    db.results[(100, 2)] = (100, 2, "Ann", 2.0, 2.0, 2, 2, 2, 2.0, "mould")
    db.sessions[(100, 7)] = {"answers": [6] * 15, "message_id": 555}
    rt = _make_rt(db, image=None)              # text-mode result → edited caption
    on_answer = _cb_handler(rt, "on_answer")

    cb = _cb(100, 7, "dq:7:15:6")              # a max scorer beats both others
    await on_answer(cb)

    final_text = cb.message.edit_text.await_args.args[0]
    assert "Grosser than 100% of this chat" in final_text


# --------------------------------------------------------------- picture flow
@pytest.mark.asyncio
async def test_start_uses_photo_when_all_images_cached():
    db = _QuizFakeDB()
    _seed_all_images(db)
    rt = _make_rt(db)
    start = _handler(rt, "start_test")
    msg = _msg(100, 7, "/disgusttest")
    await start(msg)

    msg.answer_photo.assert_awaited()               # picture mode
    msg.answer.assert_not_awaited()
    assert db.sessions[(100, 7)]["message_id"] == 556


@pytest.mark.asyncio
async def test_photo_mode_advances_with_edit_media():
    db = _QuizFakeDB()
    _seed_all_images(db)
    db.sessions[(100, 7)] = {"answers": [], "message_id": 556}
    rt = _make_rt(db)
    on_answer = _cb_handler(rt, "on_answer")

    cb = _cb(100, 7, "dq:7:0:4", photo=[SimpleNamespace(file_id="x")])
    await on_answer(cb)

    cb.message.edit_media.assert_awaited()          # swapped the picture
    cb.message.edit_text.assert_not_awaited()
    media = cb.message.edit_media.await_args.args[0]
    assert "2/16" in media.caption


@pytest.mark.asyncio
async def test_photo_mode_finalizes_by_swapping_result_image():
    db = _QuizFakeDB()
    _seed_all_images(db)
    db.sessions[(100, 7)] = {"answers": [3] * 15, "message_id": 556}
    rt = _make_rt(db, image=b"RESULT")
    on_answer = _cb_handler(rt, "on_answer")

    cb = _cb(100, 7, "dq:7:15:3", photo=[SimpleNamespace(file_id="x")])
    await on_answer(cb)

    cb.message.edit_media.assert_awaited()
    result_media = cb.message.edit_media.await_args.args[0]
    assert "Disgust Profile" in result_media.caption
    assert (100, 7) in db.results


@pytest.mark.asyncio
async def test_retake_starts_a_fresh_session():
    db = _QuizFakeDB()
    db.sessions[(100, 7)] = {"answers": [1, 2, 3], "message_id": 500}  # mid-test
    rt = _make_rt(db)
    on_retake = _cb_handler(rt, "on_retake")

    cb = _cb(100, 7, "dqx:7")
    await on_retake(cb)

    # A fresh question-1 message was sent and the session reset to empty.
    cb.message.answer.assert_awaited()
    assert db.sessions[(100, 7)]["answers"] == []


@pytest.mark.asyncio
async def test_retake_rejects_other_users():
    db = _QuizFakeDB()
    rt = _make_rt(db)
    on_retake = _cb_handler(rt, "on_retake")
    cb = _cb(100, 99, "dqx:7")
    await on_retake(cb)
    assert "not your test" in cb.answer.await_args.args[0]
    cb.message.answer.assert_not_awaited()


# --------------------------------------------------------------- warm-up
@pytest.mark.asyncio
async def test_warmup_generates_and_caches_missing_images():
    db = _QuizFakeDB()
    rt = _make_rt(db, image=b"IMG")
    await quiz_mod._warm_item_images(rt)
    assert len(db.images) == dt.N_ITEMS
    assert rt.openai.generate_image.await_count == dt.N_ITEMS


@pytest.mark.asyncio
async def test_warmup_skips_already_cached_images():
    db = _QuizFakeDB()
    _seed_all_images(db)                              # all present already
    rt = _make_rt(db, image=b"IMG")
    await quiz_mod._warm_item_images(rt)
    rt.openai.generate_image.assert_not_awaited()    # nothing to generate


# --------------------------------------------------------------- leaderboard
@pytest.mark.asyncio
async def test_leaderboard_empty_and_ranked():
    db = _QuizFakeDB()
    rt = _make_rt(db)
    board = _handler(rt, "leaderboard")

    empty_msg = _msg(100, 7, "/disgustboard")
    await board(empty_msg)
    assert "Nobody" in empty_msg.reply.await_args.args[0]

    db.results[(100, 1)] = (100, 1, "Luke", 5.0, 5.0, 5, 5, 5, 5.0, "fish")
    db.results[(100, 2)] = (100, 2, "Matt", 2.0, 2.0, 2, 2, 2, 2.0, "mould")
    db.results[(200, 3)] = (200, 3, "Other", 6.0, 6.0, 6, 6, 6, 6.0, "fish")

    ranked_msg = _msg(100, 7, "/disgustboard")
    await board(ranked_msg)
    body = ranked_msg.reply.await_args.args[0]
    assert body.index("Luke") < body.index("Matt")
    assert "Other" not in body
