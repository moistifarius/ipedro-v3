"""The picture library: filing what he saw, finding it again, sending it back."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import media_library as lib
from ipedro import vision
from ipedro.handlers import chat
from ipedro.handlers import media as media_h
from ipedro.memory.context_builder import BuiltContext
from tests.test_captcha_intercept import _msg, _rt_with

NOW = datetime.now(timezone.utc)
PHOTO = vision.Media(kind="photo", file_unique_id="u1", label="a photo", file_id="big")


# ── asking for one ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "dale send that pic of the grill",
    "where's the photo matt posted",
    "show me the screenshot again",
    "gimme the sticker from yesterday",
    "can you resend the gif of the dog",
])
def test_requests_for_a_saved_picture_are_detected(text):
    assert lib.detect_recall(text)


@pytest.mark.parametrize("text", [
    "make a meme about propane",          # a new one: the meme path
    "generate an image of a duck",        # a new one: /aigen territory
    "nice pic",                           # no ask
    "send it",                            # no picture named
    "I took a photo of the sunset",       # telling, not asking
    "",
])
def test_everything_else_is_left_alone(text):
    assert not lib.detect_recall(text)


# ── filing ───────────────────────────────────────────────────────────────────

def _rt(*, row_id=7, pgvector=True, fetch=None):
    return SimpleNamespace(
        db=SimpleNamespace(
            fetchval=AsyncMock(return_value=row_id),
            fetch=AsyncMock(return_value=fetch or []),
        ),
        memory=SimpleNamespace(
            openai=SimpleNamespace(embed=AsyncMock(return_value=[0.1, 0.2])),
            pgvector_available=pgvector,
            embeddings=SimpleNamespace(
                upsert=AsyncMock(),
                search=AsyncMock(return_value=[]),
            ),
            record_message=AsyncMock(),
        ),
        openai=SimpleNamespace(cheap_chat=AsyncMock(return_value="Filed under evidence.")),
    )


@pytest.mark.asyncio
async def test_remember_writes_the_row_and_embeds_who_and_when():
    rt = _rt()
    row_id = await lib.remember(
        rt, chat_id=42, message_id=100, media=PHOTO,
        description="a man in a bathrobe", caption="look at this idiot",
        posted_by=7, posted_by_name="Matt",
    )
    assert row_id == 7
    args = rt.db.fetchval.await_args.args
    assert "media_library" in args[0] and args[3] == "u1" and args[4] == "big"
    up = rt.memory.embeddings.upsert.await_args.args
    assert up[:3] == (42, "media", 7)
    text = up[3]
    assert "a man in a bathrobe" in text
    assert "posted by Matt" in text and "caption: look at this idiot" in text


@pytest.mark.asyncio
async def test_a_message_already_filed_is_not_embedded_twice():
    rt = _rt(row_id=None)               # ON CONFLICT DO NOTHING → no id back
    assert await lib.remember(
        rt, chat_id=42, message_id=100, media=PHOTO, description="x",
        caption=None, posted_by=7, posted_by_name="Matt",
    ) is None
    rt.memory.embeddings.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_db_failure_on_filing_is_swallowed():
    rt = _rt()
    rt.db.fetchval = AsyncMock(side_effect=RuntimeError("db down"))
    assert await lib.remember(
        rt, chat_id=42, message_id=100, media=PHOTO, description="x",
        caption=None, posted_by=7, posted_by_name="Matt",
    ) is None


# ── finding ──────────────────────────────────────────────────────────────────

def _row(i, desc, kind="photo", who="Matt", ago_min=60):
    return {"id": i, "file_id": f"file{i}", "kind": kind, "description": desc,
            "caption": None, "posted_by_name": who,
            "created_at": NOW - timedelta(minutes=ago_min)}


@pytest.mark.asyncio
async def test_vector_search_keeps_only_media_hits_in_order():
    rt = _rt(fetch=[_row(3, "a propane grill"), _row(9, "a dog")])
    rt.memory.embeddings.search = AsyncMock(return_value=[
        {"ref_kind": "message", "ref_id": 1, "similarity": 0.9},   # not a picture
        {"ref_kind": "media", "ref_id": 3, "similarity": 0.8},
        {"ref_kind": "media", "ref_id": 9, "similarity": 0.4},
    ])
    out = await lib.search(rt, 42, "the grill")
    assert [r["id"] for r in out] == [3, 9]
    assert out[0]["similarity"] == 0.8
    # the row fetch asked for exactly the media ids, in hit order
    assert rt.db.fetch.await_args.args[2] == [3, 9]


@pytest.mark.asyncio
async def test_keyword_fallback_without_pgvector():
    rt = _rt(pgvector=False, fetch=[_row(5, "a propane grill on a deck")])
    out = await lib.search(rt, 42, "the grill pic")
    assert out and out[0]["id"] == 5 and out[0]["similarity"] is None
    pattern = rt.db.fetch.await_args.args[2]
    assert "grill" in pattern and "the" not in pattern     # short words dropped


@pytest.mark.asyncio
async def test_keyword_fallback_with_nothing_distinctive_finds_nothing():
    rt = _rt(pgvector=False)
    assert await lib.search(rt, 42, "it") == []
    rt.db.fetch.assert_not_awaited()


# ── sending back ─────────────────────────────────────────────────────────────

def _cfg(memory=True):
    return SimpleNamespace(memory_enabled=memory)


def _tg_msg():
    m = _msg(text="dale send that pic of the grill")
    for name in ("reply_photo", "reply_animation", "reply_video",
                 "reply_video_note", "reply_sticker", "reply_document"):
        setattr(m, name, AsyncMock(return_value=SimpleNamespace(message_id=55)))
    return m


@pytest.mark.asyncio
async def test_a_confident_match_is_sent_with_a_caption_and_remembered(monkeypatch):
    rt = _rt()
    monkeypatch.setattr(lib, "search", AsyncMock(return_value=[
        {**_row(3, "a propane grill"), "similarity": 0.8},
    ]))
    msg = _tg_msg()
    assert await lib.recall(rt, msg, _cfg(), "send that pic of the grill") is True
    msg.reply_photo.assert_awaited_once_with("file3", caption="Filed under evidence.")
    recorded = rt.memory.record_message.await_args.kwargs
    assert recorded["role"] == "assistant" and "sent back a saved photo" in recorded["content"]
    assert recorded["message_id"] == 55


@pytest.mark.asyncio
async def test_a_weak_match_is_not_sent(monkeypatch):
    """Below the floor the 'best' match is a guess; he says so instead."""
    rt = _rt()
    monkeypatch.setattr(lib, "search", AsyncMock(return_value=[
        {**_row(3, "a propane grill"), "similarity": 0.1},
    ]))
    msg = _tg_msg()
    assert await lib.recall(rt, msg, _cfg(), "that pic of my cousin") is False
    msg.reply_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_kind_uses_its_own_send(monkeypatch):
    rt = _rt()
    for kind, method, captioned in (
        ("gif", "reply_animation", True), ("video", "reply_video", True),
        ("sticker", "reply_sticker", False), ("image", "reply_document", True),
    ):
        monkeypatch.setattr(lib, "search", AsyncMock(return_value=[
            {**_row(3, "x", kind=kind), "similarity": 0.9},
        ]))
        msg = _tg_msg()
        assert await lib.recall(rt, msg, _cfg(memory=False), "send it") is True
        call = getattr(msg, method).await_args
        assert call.args[0] == "file3"
        assert ("caption" in call.kwargs) is captioned


@pytest.mark.asyncio
async def test_caption_falls_back_to_plain_when_the_model_is_out(monkeypatch):
    rt = _rt()
    rt.openai.cheap_chat = AsyncMock(return_value=None)
    monkeypatch.setattr(lib, "search", AsyncMock(return_value=[
        {**_row(3, "a grill", ago_min=3 * 24 * 60), "similarity": 0.9},
    ]))
    msg = _tg_msg()
    await lib.recall(rt, msg, _cfg(memory=False), "send it")
    caption = msg.reply_photo.await_args.kwargs["caption"]
    assert caption.startswith("Matt,") and "day" in caption


@pytest.mark.asyncio
async def test_a_telegram_refusal_is_a_miss_not_a_crash(monkeypatch):
    rt = _rt()
    monkeypatch.setattr(lib, "search", AsyncMock(return_value=[
        {**_row(3, "x"), "similarity": 0.9},
    ]))
    msg = _tg_msg()
    msg.reply_photo = AsyncMock(side_effect=RuntimeError("file expired"))
    assert await lib.recall(rt, msg, _cfg(memory=False), "send it") is False


# ── through on_message ───────────────────────────────────────────────────────

def _chat_rt(monkeypatch):
    rt = _rt_with()
    cfg = rt.chats.get_config.return_value
    cfg.response_policy = "mention"
    cfg.memory_enabled = True
    for field in ("monthly_recap_enabled", "share_photo_enabled",
                  "comic_enabled", "fortune_enabled", "ether_enabled"):
        setattr(cfg, field, False)
    rt.memory = SimpleNamespace(record_message=AsyncMock(),
                                recent_messages=AsyncMock(return_value=[]))
    rt.openai = SimpleNamespace(chat=AsyncMock(return_value="sh-sha"),
                                cheap_completion=AsyncMock(return_value="NO"))
    captured = {}

    async def fake_build(**kwargs):
        captured.update(kwargs)
        return BuiltContext(messages=[{"role": "user", "content": "x"}], tokens=1)

    monkeypatch.setattr(chat, "build_context", fake_build)
    monkeypatch.setattr(chat, "maybe_summarize", AsyncMock())
    monkeypatch.setattr(chat, "resolve_impersonation", AsyncMock(return_value=None))
    return rt, captured


def _handler(rt):
    router = chat.build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == "on_message")


@pytest.mark.asyncio
async def test_a_hit_sends_the_picture_instead_of_a_text_reply(monkeypatch):
    rt, _ = _chat_rt(monkeypatch)
    monkeypatch.setattr(lib, "recall", AsyncMock(return_value=True))
    msg = _msg(text="dale send that pic of the grill")
    msg.answer = AsyncMock()
    await _handler(rt)(msg)
    lib.recall.assert_awaited_once()
    assert lib.recall.await_args.args[3] == "dale send that pic of the grill"
    rt.openai.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_miss_lets_him_say_so_in_his_own_voice(monkeypatch):
    rt, captured = _chat_rt(monkeypatch)
    monkeypatch.setattr(lib, "recall", AsyncMock(return_value=False))
    msg = _msg(text="dale send that pic of the grill")
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    rt.openai.chat.assert_awaited_once()
    assert "nothing matches" in (captured["extra_system"] or "")


@pytest.mark.asyncio
async def test_the_library_is_memory_and_respects_the_switch(monkeypatch):
    rt, _ = _chat_rt(monkeypatch)
    rt.chats.get_config.return_value.memory_enabled = False
    monkeypatch.setattr(lib, "recall", AsyncMock(return_value=True))
    msg = _msg(text="dale send that pic of the grill")
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    lib.recall.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_described_photo_is_filed_with_who_posted_it(monkeypatch):
    rt, _ = _chat_rt(monkeypatch)
    rt.chats.get_config.return_value.vision_enabled = True
    monkeypatch.setattr(chat, "should_respond", lambda *a, **k: False)
    monkeypatch.setattr(vision, "look", AsyncMock(return_value=vision.Seen(
        media=PHOTO, description="a propane grill", note="[photo: a propane grill]",
    )))
    remember = AsyncMock(return_value=1)
    monkeypatch.setattr(lib, "remember", remember)
    msg = _msg(text=None)
    msg.text = None
    msg.caption = "look at this"
    msg.photo = [SimpleNamespace(file_id="big", file_unique_id="u1", file_size=1)]
    await _handler(rt)(msg)
    kw = remember.await_args.kwargs
    assert kw["description"] == "a propane grill"
    assert kw["caption"] == "look at this" and kw["message_id"] == 100
    assert kw["posted_by_name"]


# ── the commands ─────────────────────────────────────────────────────────────

def _cmd(rt, name):
    router = media_h.build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == name)


@pytest.mark.asyncio
async def test_pic_command_searches_and_reports_a_miss(monkeypatch):
    rt = _rt_with()
    rt.chats.get_config.return_value.memory_enabled = True
    monkeypatch.setattr(lib, "recall", AsyncMock(return_value=False))
    msg = _msg(text="/pic the grill")
    await _cmd(rt, "pic")(msg)
    assert lib.recall.await_args.args[3] == "the grill"
    assert "Nothing saved" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_pics_lists_recent_with_who_and_when(monkeypatch):
    rt = _rt_with()
    rt.chats.get_config.return_value.memory_enabled = True
    monkeypatch.setattr(lib, "recent", AsyncMock(return_value=[
        _row(1, "a propane grill", who="Matt", ago_min=90),
    ]))
    msg = _msg(text="/pics")
    await _cmd(rt, "pics")(msg)
    out = msg.reply.await_args.args[0]
    assert "Matt" in out and "a propane grill" in out and "hour" in out
