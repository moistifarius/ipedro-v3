"""/dalegif — capturing GIFs from Telegram without touching a file or a URL."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import dale_gifs as dg
from ipedro.handlers.dale import build_router
from tests.test_dale_gifs import _FakeDB

ADMIN = 7


def _rt(db=None):
    return SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset({ADMIN})),
        db=db if db is not None else _FakeDB(),
    )


def _handler(rt):
    router = build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == "dalegif")


def _anim(file_id="FID", unique="U1"):
    return SimpleNamespace(file_id=file_id, file_unique_id=unique)


def _msg(*, text=None, caption=None, user_id=ADMIN, animation=None,
         document=None, video=None, sticker=None, reply_to=None):
    return SimpleNamespace(
        chat=SimpleNamespace(id=42, type="group"),
        from_user=SimpleNamespace(id=user_id),
        text=text, caption=caption,
        animation=animation, document=document, video=video, sticker=sticker,
        photo=None, reply_to_message=reply_to,
        reply=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        reply_animation=AsyncMock(return_value=SimpleNamespace(
            message_id=2, animation=_anim("SENT", "US"))),
    )


def _said(msg) -> str:
    return msg.reply.await_args.args[0]


# ── the two capture paths ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_capture_by_replying_to_a_gif():
    """The primary path: Telegram's GIF picker can't attach a caption, so you
    send the GIF and reply to it."""
    rt = _rt()
    gif_msg = _msg(animation=_anim("FID1", "UNIQ1"))
    msg = _msg(text="/dalegif pocketsand paranoia", reply_to=gif_msg)

    await _handler(rt)(msg)

    assert len(rt.db.rows) == 1
    assert rt.db.rows[0]["file_id"] == "FID1"
    assert rt.db.rows[0]["file_unique_id"] == "UNIQ1"
    assert rt.db.rows[0]["tags"] == ["pocketsand", "paranoia"]
    assert "Filed" in _said(msg)


@pytest.mark.asyncio
async def test_capture_by_captioning_the_gif():
    """One-step path where the client allows a caption. Args must come from
    `caption`, not `text` — this is the bug this design most invites."""
    rt = _rt()
    msg = _msg(caption="/dalegif conspiracy", animation=_anim("FID2", "UNIQ2"))

    await _handler(rt)(msg)

    assert rt.db.rows[0]["file_id"] == "FID2"
    assert rt.db.rows[0]["tags"] == ["conspiracy"]


@pytest.mark.asyncio
async def test_capture_accepts_a_gif_document():
    """A .gif uploaded from the file picker arrives as a document."""
    rt = _rt()
    doc = SimpleNamespace(file_id="DOC", file_unique_id="UD",
                          mime_type="image/gif")
    msg = _msg(text="/dalegif pocketsand", document=doc)

    await _handler(rt)(msg)
    assert rt.db.rows[0]["file_id"] == "DOC"


@pytest.mark.asyncio
async def test_animation_wins_over_document_on_the_same_message():
    """Telegram sets `document` alongside `animation`; the animation is the
    one sendAnimation will accept."""
    rt = _rt()
    doc = SimpleNamespace(file_id="DOC", file_unique_id="UD",
                          mime_type="video/mp4")
    msg = _msg(text="/dalegif x", animation=_anim("ANIM", "UA"), document=doc)

    await _handler(rt)(msg)
    assert rt.db.rows[0]["file_id"] == "ANIM"


@pytest.mark.asyncio
async def test_a_gif_always_means_add_even_when_args_look_like_a_subcommand():
    rt = _rt()
    msg = _msg(text="/dalegif list", animation=_anim("FID", "UQ"))

    await _handler(rt)(msg)
    assert len(rt.db.rows) == 1 and rt.db.rows[0]["tags"] == ["list"]


@pytest.mark.asyncio
async def test_capture_needs_at_least_one_tag():
    rt = _rt()
    msg = _msg(text="/dalegif", animation=_anim())

    await _handler(rt)(msg)
    assert rt.db.rows == []
    assert "tag" in _said(msg).lower()


# ── refusals ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("kind,kwargs", [
    ("video", {"video": SimpleNamespace(file_id="V", file_unique_id="UV")}),
    ("sticker", {"sticker": SimpleNamespace(file_id="S", file_unique_id="US")}),
])
async def test_unusable_media_is_refused_honestly(kind, kwargs):
    """Telegram rejects a video/sticker file_id from sendAnimation, so storing
    one would create a row that can never be sent."""
    rt = _rt()
    msg = _msg(text="/dalegif pocketsand", **kwargs)

    await _handler(rt)(msg)
    assert rt.db.rows == []
    assert kind in _said(msg)


@pytest.mark.asyncio
async def test_non_admin_gets_silence():
    rt = _rt()
    msg = _msg(text="/dalegif pocketsand", user_id=999,
               animation=_anim())

    await _handler(rt)(msg)
    msg.reply.assert_not_awaited()
    assert rt.db.rows == []


# ── duplicates retag ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_saving_the_same_gif_again_retags_and_says_so():
    rt = _rt()
    handler = _handler(rt)
    await handler(_msg(text="/dalegif pocketsand", animation=_anim("F", "SAME")))
    second = _msg(text="/dalegif paranoia", animation=_anim("F", "SAME"))
    await handler(second)

    assert len(rt.db.rows) == 1
    assert rt.db.rows[0]["tags"] == ["pocketsand", "paranoia"]
    said = _said(second)
    assert "Already had" in said and "paranoia" in said


# ── subcommands ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_empty_points_at_seed():
    rt = _rt()
    msg = _msg(text="/dalegif list")
    await _handler(rt)(msg)
    assert "seed" in _said(msg)


@pytest.mark.asyncio
async def test_list_shows_ids_tags_and_counts():
    rt = _rt()
    await dg.add(rt.db, ["pocketsand"], file_id="A", file_unique_id="U1")
    msg = _msg(text="/dalegif list")
    await _handler(rt)(msg)
    said = _said(msg)
    assert "#1" in said and "pocketsand" in said


@pytest.mark.asyncio
async def test_list_can_filter_by_tag():
    rt = _rt()
    await dg.add(rt.db, ["pocketsand"], file_id="A", file_unique_id="U1")
    msg = _msg(text="/dalegif list propane")
    await _handler(rt)(msg)
    assert "propane" in _said(msg)


@pytest.mark.asyncio
async def test_remove_reports_hit_and_miss():
    rt = _rt()
    await dg.add(rt.db, ["pocketsand"], file_id="A", file_unique_id="U1")

    hit = _msg(text="/dalegif remove 1")
    await _handler(rt)(hit)
    assert "Deleted" in _said(hit)

    miss = _msg(text="/dalegif remove 1")
    await _handler(rt)(miss)
    assert "No GIF" in _said(miss)


@pytest.mark.asyncio
async def test_remove_rejects_a_non_number():
    rt = _rt()
    msg = _msg(text="/dalegif remove banana")
    await _handler(rt)(msg)
    assert "Usage" in _said(msg)


@pytest.mark.asyncio
async def test_test_subcommand_sends_a_gif():
    rt = _rt()
    await dg.add(rt.db, ["pocketsand"], file_id="A", file_unique_id="U1")
    msg = _msg(text="/dalegif test pocketsand")
    await _handler(rt)(msg)
    msg.reply_animation.assert_awaited()


@pytest.mark.asyncio
async def test_test_subcommand_says_so_when_empty():
    rt = _rt()
    msg = _msg(text="/dalegif test pocketsand")
    await _handler(rt)(msg)
    msg.reply_animation.assert_not_awaited()
    assert "Nothing to send" in _said(msg)


@pytest.mark.asyncio
async def test_bare_command_shows_usage():
    rt = _rt()
    msg = _msg(text="/dalegif")
    await _handler(rt)(msg)
    assert "reply /dalegif" in _said(msg)


@pytest.mark.asyncio
async def test_seed_reports_counts(monkeypatch):
    monkeypatch.setattr(dg, "_SEED_GIFS", (
        ("https://media.tenor.com/a.gif", ("pocketsand",)),
        ("https://media.tenor.com/b.gif", ("paranoia",)),
    ))
    rt = _rt()
    first = _msg(text="/dalegif seed")
    await _handler(rt)(first)
    assert "2 added" in _said(first)

    again = _msg(text="/dalegif seed")
    await _handler(rt)(again)
    assert "0 added" in _said(again)     # re-running is a no-op
    assert len(rt.db.rows) == 2
