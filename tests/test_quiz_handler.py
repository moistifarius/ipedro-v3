"""Flow tests for the quiz engine (via the disgust + dark-triad quizzes).

Fakes the quiz_* tables in memory (keyed by quiz_id, reproducing the atomic
cardinality-guarded append) so the interactive contract is exercised without
Postgres: menu, start → answer → advance in text and picture mode, ownership,
stale/expired taps, cancel, finalize (image + fallback), percentile, retake,
warm-up, and the leaderboard — across quizzes with different scales.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.quizzes import engine, registry

DISGUST = registry.get("disgust")
DARK = registry.get("darktriad")


@pytest.fixture(autouse=True)
def _no_background_warmup(monkeypatch):
    monkeypatch.setattr(engine, "_kick_warmup", lambda rt, quiz: None)


class _FakeDB:
    def __init__(self) -> None:
        self.sessions: dict[tuple, dict] = {}
        self.results: dict[tuple, dict] = {}
        self.images: dict[tuple, bytes] = {}

    async def execute(self, query, *args):
        if "INSERT INTO quiz_sessions" in query:
            quiz_id, chat_id, user_id, message_id = args
            self.sessions[(quiz_id, chat_id, user_id)] = {
                "answers": [], "message_id": message_id}
            return "INSERT 0 1"
        if "DELETE FROM quiz_sessions" in query:
            self.sessions.pop((args[0], args[1], args[2]), None)
            return "DELETE 1"
        if "INSERT INTO quiz_results" in query:
            quiz_id, chat_id, user_id, name, headline, summary, _detail = args
            self.results[(quiz_id, chat_id, user_id)] = {
                "display_name": name, "headline_score": headline, "summary": summary}
            return "INSERT 0 1"
        if "INSERT INTO quiz_item_images" in query:
            quiz_id, item_key, png = args
            self.images.setdefault((quiz_id, item_key), png)
            return "INSERT 0 1"
        return "OK"

    async def fetchrow(self, query, *args):
        if "UPDATE quiz_sessions" in query and "array_append" in query:
            value, quiz_id, chat_id, user_id, idx = args
            sess = self.sessions.get((quiz_id, chat_id, user_id))
            if sess is None or len(sess["answers"]) != idx:
                return None
            sess["answers"].append(value)
            return {"answers": list(sess["answers"])}
        if "COUNT(*) AS total" in query and "quiz_results" in query:
            quiz_id, chat_id, score, user_id = args
            others = [v for k, v in self.results.items()
                      if k[0] == quiz_id and k[1] == chat_id and k[2] != user_id]
            below = sum(1 for v in others if v["headline_score"] < score)
            return {"total": len(others), "below": below}
        return None

    async def fetchval(self, query, *args):
        if "SELECT 1 FROM quiz_sessions" in query:
            return 1 if (args[0], args[1], args[2]) in self.sessions else None
        if "COUNT(*) FROM quiz_item_images" in query:
            quiz_id, keys = args
            return sum(1 for (qid, k) in self.images if qid == quiz_id and k in keys)
        if "SELECT 1 FROM quiz_item_images" in query:
            return 1 if (args[0], args[1]) in self.images else None
        if "SELECT png FROM quiz_item_images" in query:
            return self.images.get((args[0], args[1]))
        return None

    async def fetch(self, query, *args):
        if "FROM quiz_results" in query:
            quiz_id, chat_id = args
            rows = [dict(v) for k, v in self.results.items()
                    if k[0] == quiz_id and k[1] == chat_id]
            rows.sort(key=lambda r: -r["headline_score"])
            return rows[:15]
        return []


def _seed_images(db, quiz):
    for it in quiz.items:
        db.images[(quiz.id, it.key)] = b"IMG"


def _rt(db, *, image=b"PNG", verdict="you gross freak", admin=frozenset()):
    cfg = SimpleNamespace()
    chats = SimpleNamespace(upsert_chat=AsyncMock(),
                            get_config=AsyncMock(return_value=cfg),
                            upsert_default_config=AsyncMock(return_value=cfg))
    return SimpleNamespace(
        settings=SimpleNamespace(admin_ids=admin),
        db=db, chats=chats, users=SimpleNamespace(upsert_user=AsyncMock()),
        openai=SimpleNamespace(chat=AsyncMock(return_value=verdict),
                               generate_image=AsyncMock(return_value=image)),
        bot=SimpleNamespace(),
    )


def _user(uid, name="Matt"):
    return SimpleNamespace(id=uid, is_bot=False, username=name.lower(),
                           first_name=name, last_name=None)


def _msg(chat_id, uid, text="/x"):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="group", title="t"),
        from_user=_user(uid), text=text, caption=None, message_id=1,
        answer=AsyncMock(return_value=SimpleNamespace(message_id=555)),
        answer_photo=AsyncMock(return_value=SimpleNamespace(message_id=556)),
        reply=AsyncMock(),
    )


def _cb(chat_id, uid, data, *, photo=None):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="group"), photo=photo,
        edit_text=AsyncMock(), edit_caption=AsyncMock(), edit_media=AsyncMock(),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=557)),
        answer_photo=AsyncMock(return_value=SimpleNamespace(message_id=558)),
    )
    return SimpleNamespace(data=data, from_user=_user(uid), message=message,
                           answer=AsyncMock())


# --------------------------------------------------------------- menu / start
@pytest.mark.asyncio
async def test_menu_lists_every_quiz():
    rt = _rt(_FakeDB())
    msg = _msg(100, 7, "/tests")
    await engine.show_menu(rt, msg)
    body = msg.answer.await_args.args[0]
    for q in registry.all_quizzes():
        assert q.title in body


@pytest.mark.asyncio
async def test_start_creates_session_text_mode():
    db = _FakeDB()
    rt = _rt(db)
    await engine.start_command(rt, DISGUST, _msg(100, 7))
    assert ("disgust", 100, 7) in db.sessions
    assert db.sessions[("disgust", 100, 7)]["answers"] == []


@pytest.mark.asyncio
async def test_menu_go_button_starts_chosen_quiz():
    db = _FakeDB()
    rt = _rt(db)
    await engine.on_go(rt, _cb(100, 7, "qgo:darktriad"))
    assert ("darktriad", 100, 7) in db.sessions
    await engine.on_go(rt, _cb(100, 7, "qgo:nonesuch"))   # unknown id → ignored, no crash


# --------------------------------------------------------------- answering
@pytest.mark.asyncio
async def test_answer_advances():
    db = _FakeDB()
    db.sessions[("disgust", 100, 7)] = {"answers": [], "message_id": 555}
    rt = _rt(db)
    await engine.on_answer(rt, _cb(100, 7, "q:disgust:7:0:4"))
    assert db.sessions[("disgust", 100, 7)]["answers"] == [4]


@pytest.mark.asyncio
async def test_scale_ceiling_is_per_quiz():
    """Dark Triad is 1-7; a 7 is valid, an 8 is rejected."""
    db = _FakeDB()
    db.sessions[("darktriad", 100, 7)] = {"answers": [], "message_id": 5}
    rt = _rt(db)
    await engine.on_answer(rt, _cb(100, 7, "q:darktriad:7:0:7"))
    assert db.sessions[("darktriad", 100, 7)]["answers"] == [7]
    await engine.on_answer(rt, _cb(100, 7, "q:darktriad:7:1:8"))   # out of range
    assert db.sessions[("darktriad", 100, 7)]["answers"] == [7]    # unchanged


@pytest.mark.asyncio
async def test_only_owner_can_answer():
    db = _FakeDB()
    db.sessions[("disgust", 100, 7)] = {"answers": [], "message_id": 555}
    rt = _rt(db)
    cb = _cb(100, 99, "q:disgust:7:0:4")
    await engine.on_answer(rt, cb)
    assert db.sessions[("disgust", 100, 7)]["answers"] == []
    assert "not your test" in cb.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_stale_tap_ignored():
    db = _FakeDB()
    db.sessions[("disgust", 100, 7)] = {"answers": [3, 3], "message_id": 5}
    rt = _rt(db)
    cb = _cb(100, 7, "q:disgust:7:0:5")
    await engine.on_answer(rt, cb)
    assert db.sessions[("disgust", 100, 7)]["answers"] == [3, 3]
    assert cb.answer.await_args.args == ()


@pytest.mark.asyncio
async def test_expired_session():
    rt = _rt(_FakeDB())
    cb = _cb(100, 7, "q:disgust:7:0:5")
    await engine.on_answer(rt, cb)
    assert "expired" in cb.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_cancel_clears_session():
    db = _FakeDB()
    db.sessions[("disgust", 100, 7)] = {"answers": [1], "message_id": 5}
    rt = _rt(db)
    cb = _cb(100, 7, "q:disgust:7:x")
    await engine.on_answer(rt, cb)
    assert ("disgust", 100, 7) not in db.sessions
    assert "cancelled" in cb.message.edit_text.await_args.args[0].lower()


# --------------------------------------------------------------- finalize
@pytest.mark.asyncio
async def test_finalize_image_result_and_store():
    db = _FakeDB()
    db.sessions[("disgust", 100, 7)] = {"answers": [3] * 15, "message_id": 5}
    rt = _rt(db, image=b"RES")
    cb = _cb(100, 7, "q:disgust:7:15:3")
    await engine.on_answer(rt, cb)

    assert cb.message.answer_photo.await_count == 1
    caption = cb.message.answer_photo.await_args.kwargs["caption"]
    assert "Disgust Test" in caption
    kb = cb.message.answer_photo.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "qr:disgust:7"
    assert ("disgust", 100, 7) in db.results
    assert ("disgust", 100, 7) not in db.sessions


@pytest.mark.asyncio
async def test_finalize_text_fallback_when_no_image():
    db = _FakeDB()
    db.sessions[("disgust", 100, 7)] = {"answers": [3] * 15, "message_id": 5}
    rt = _rt(db, image=None)
    cb = _cb(100, 7, "q:disgust:7:15:3")
    await engine.on_answer(rt, cb)
    cb.message.answer_photo.assert_not_awaited()
    txt = cb.message.edit_text.await_args.args[0]
    assert "For fun, not a diagnosis." in txt


@pytest.mark.asyncio
async def test_percentile_line_for_ranked_quiz():
    db = _FakeDB()
    db.results[("disgust", 100, 1)] = {
        "display_name": "Lo", "headline_score": 1.0, "summary": "x"}
    db.results[("disgust", 100, 2)] = {
        "display_name": "Mid", "headline_score": 2.0, "summary": "y"}
    db.sessions[("disgust", 100, 7)] = {"answers": [6] * 15, "message_id": 5}
    rt = _rt(db, image=None)
    cb = _cb(100, 7, "q:disgust:7:15:6")
    await engine.on_answer(rt, cb)
    assert "Ranks above 100% of this chat" in cb.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_unranked_quiz_has_no_percentile():
    db = _FakeDB()
    db.results[("bigfive", 100, 1)] = {
        "display_name": "A", "headline_score": 1.0, "summary": "x"}
    db.sessions[("bigfive", 100, 7)] = {"answers": [5] * 9, "message_id": 5}
    rt = _rt(db, image=None)
    cb = _cb(100, 7, "q:bigfive:7:9:5")                         # the 10th answer
    await engine.on_answer(rt, cb)
    assert ("bigfive", 100, 7) in db.results
    # bigfive has no leaderboard → the result shows no percentile line.
    assert "Ranks above" not in cb.message.edit_text.await_args.args[0]


# --------------------------------------------------------------- picture flow
@pytest.mark.asyncio
async def test_photo_mode_start_and_advance():
    db = _FakeDB()
    _seed_images(db, DISGUST)
    rt = _rt(db)
    msg = _msg(100, 7)
    await engine.start_command(rt, DISGUST, msg)
    msg.answer_photo.assert_awaited()
    msg.answer.assert_not_awaited()

    cb = _cb(100, 7, "q:disgust:7:0:4", photo=[SimpleNamespace(file_id="x")])
    await engine.on_answer(rt, cb)
    cb.message.edit_media.assert_awaited()
    assert "2/16" in cb.message.edit_media.await_args.args[0].caption


# --------------------------------------------------------------- retake
@pytest.mark.asyncio
async def test_retake_resets_session():
    db = _FakeDB()
    db.sessions[("disgust", 100, 7)] = {"answers": [1, 2, 3], "message_id": 9}
    rt = _rt(db)
    cb = _cb(100, 7, "qr:disgust:7")
    await engine.on_retake(rt, cb)
    cb.message.answer.assert_awaited()
    assert db.sessions[("disgust", 100, 7)]["answers"] == []


@pytest.mark.asyncio
async def test_retake_rejects_other_user():
    rt = _rt(_FakeDB())
    cb = _cb(100, 99, "qr:disgust:7")
    await engine.on_retake(rt, cb)
    assert "not your test" in cb.answer.await_args.args[0]
    cb.message.answer.assert_not_awaited()


# --------------------------------------------------------------- warm-up
@pytest.mark.asyncio
async def test_warmup_generates_and_caches():
    db = _FakeDB()
    rt = _rt(db, image=b"IMG")
    await engine._warm_item_images(rt, DARK)
    assert sum(1 for (qid, _k) in db.images if qid == "darktriad") == DARK.n_items
    assert rt.openai.generate_image.await_count == DARK.n_items


# --------------------------------------------------------------- leaderboard
@pytest.mark.asyncio
async def test_leaderboard_empty_and_ranked():
    db = _FakeDB()
    rt = _rt(db)
    empty = _msg(100, 7, "/disgustboard")
    await engine.leaderboard(rt, DISGUST, empty)
    assert "Nobody" in empty.reply.await_args.args[0]

    db.results[("disgust", 100, 1)] = {
        "display_name": "Luke", "headline_score": 5.0, "summary": "squeamish"}
    db.results[("disgust", 100, 2)] = {
        "display_name": "Matt", "headline_score": 2.0, "summary": "unbothered"}
    db.results[("darktriad", 100, 3)] = {
        "display_name": "Other", "headline_score": 6.0, "summary": "menace"}
    ranked = _msg(100, 7, "/disgustboard")
    await engine.leaderboard(rt, DISGUST, ranked)
    body = ranked.reply.await_args.args[0]
    assert body.index("Luke") < body.index("Matt")
    assert "Other" not in body            # scoped to this quiz
