"""The bot looks at pictures: extraction, description, caching, fallbacks.

The load-bearing rule is the last group: a machine-written description of
a photo must never be treated as something the user typed. Otherwise the
bot's own words about an image fire the automod table, the cat-fact
intercept and the meme detector, and a picture of a cat gets a canned
line instead of an actual reaction.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import vision
from ipedro.handlers import chat
from tests.test_captcha_intercept import _msg, _rt_with

# Real magic bytes — sniffing is what decides whether a provider can look
# at the file at all, so the fixtures have to be honest about their format.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x01\x00\x00" + b"WEBP" + b"\x00" * 64
TGS = b"\x1f\x8b\x08\x00" + b"\x00" * 64          # gzipped Lottie, not an image


# ── format sniffing ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("data,expected", [
    (JPEG, "image/jpeg"),
    (PNG, "image/png"),
    (GIF, "image/gif"),
    (WEBP, "image/webp"),
    (TGS, None),
    (b"", None),
    (b"RIFFxxxxAVI ", None),      # RIFF, but not a WEBP
])
def test_sniff_image_type(data, expected):
    assert vision.sniff_image_type(data) == expected


# ── which media, and how to look at it ───────────────────────────────────────

def _photo(**kw):
    return SimpleNamespace(file_id="f", file_unique_id="u", **kw)


def _blank_msg(**kw):
    base = dict(photo=None, sticker=None, animation=None, video=None,
                video_note=None, document=None, audio=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_photo_uses_the_largest_size():
    """Telegram sends several resolutions; the small one loses the text in
    a meme, which is usually the whole point of the image."""
    small = SimpleNamespace(file_id="small", file_unique_id="us",
                            file_size=1000)
    big = SimpleNamespace(file_id="big", file_unique_id="ub",
                          file_size=90_000)
    media = vision.extract_media(_blank_msg(photo=[small, big]))
    assert media.kind == "photo"
    assert media.file_id == "big"
    assert media.viewable


def test_static_sticker_is_looked_at_directly():
    """A static sticker IS a WEBP image — no thumbnail round-trip."""
    sticker = SimpleNamespace(
        file_id="sticker", file_unique_id="us", emoji="😂",
        set_name="Pack", is_animated=False, is_video=False,
        thumbnail=SimpleNamespace(file_id="thumb"),
    )
    media = vision.extract_media(_blank_msg(sticker=sticker))
    assert media.file_id == "sticker"
    assert "😂" in media.label and "Pack" in media.label


@pytest.mark.parametrize("flag", ["is_animated", "is_video"])
def test_moving_sticker_falls_back_to_its_thumbnail(flag):
    """An animated sticker is Lottie JSON and a video sticker is webm —
    neither is an image, so we look at the still frame instead."""
    kw = dict(is_animated=False, is_video=False)
    kw[flag] = True
    sticker = SimpleNamespace(
        file_id="sticker", file_unique_id="us", emoji=None, set_name=None,
        thumbnail=SimpleNamespace(file_id="thumb"), **kw,
    )
    assert vision.extract_media(_blank_msg(sticker=sticker)).file_id == "thumb"


def test_moving_sticker_with_no_thumbnail_is_labeled_only():
    sticker = SimpleNamespace(
        file_id="s", file_unique_id="us", emoji="🔥", set_name=None,
        is_animated=True, is_video=False, thumbnail=None,
    )
    media = vision.extract_media(_blank_msg(sticker=sticker))
    assert not media.viewable
    assert vision.note(media, None) == "[a sticker (🔥)]"


def test_gif_and_video_use_thumbnails():
    gif = SimpleNamespace(file_id="g", file_unique_id="ug",
                          thumbnail=SimpleNamespace(file_id="gthumb"))
    assert vision.extract_media(_blank_msg(animation=gif)).file_id == "gthumb"
    video = SimpleNamespace(file_id="v", file_unique_id="uv", duration=75,
                            thumbnail=SimpleNamespace(file_id="vthumb"))
    media = vision.extract_media(_blank_msg(video=video))
    assert media.file_id == "vthumb"
    assert "1:15" in media.label


def test_image_sent_as_a_file_is_still_an_image():
    doc = SimpleNamespace(file_id="d", file_unique_id="ud",
                          mime_type="image/png", file_name="cat.png",
                          thumbnail=None)
    assert vision.extract_media(_blank_msg(document=doc)).file_id == "d"


def test_non_image_document_is_labeled_not_viewed():
    doc = SimpleNamespace(file_id="d", file_unique_id="ud",
                          mime_type="application/pdf", file_name="taxes.pdf",
                          thumbnail=None)
    media = vision.extract_media(_blank_msg(document=doc))
    assert not media.viewable
    assert "taxes.pdf" in media.label


def test_audio_is_labeled():
    audio = SimpleNamespace(file_id="a", file_unique_id="ua", duration=200,
                            performer="Boomhauer", title="Dang Ol")
    media = vision.extract_media(_blank_msg(audio=audio))
    assert not media.viewable
    assert "Boomhauer" in media.label and "3:20" in media.label


def test_a_plain_text_message_has_no_media():
    assert vision.extract_media(_blank_msg()) is None


# ── describing ───────────────────────────────────────────────────────────────

def _vision_rt(*, described="a man in a bathrobe", cached=None):
    async def _download_file(path, destination):
        destination.write(JPEG)

    return SimpleNamespace(
        db=SimpleNamespace(
            fetchrow=AsyncMock(
                return_value={"description": cached} if cached else None,
            ),
            execute=AsyncMock(),
        ),
        bot=SimpleNamespace(
            get_file=AsyncMock(return_value=SimpleNamespace(
                file_path="p/f.jpg", file_size=1234,
            )),
            download_file=AsyncMock(side_effect=_download_file),
        ),
        openai=SimpleNamespace(
            describe_image=AsyncMock(return_value=described),
        ),
    )


def _photo_msg():
    return SimpleNamespace(
        chat=SimpleNamespace(id=42),
        photo=[SimpleNamespace(file_id="big", file_unique_id="u1",
                               file_size=90_000)],
        sticker=None, animation=None, video=None, video_note=None,
        document=None, audio=None,
    )


@pytest.mark.asyncio
async def test_describe_returns_a_bracketed_note():
    rt = _vision_rt()
    assert await vision.describe(rt, _photo_msg()) == (
        "[photo: a man in a bathrobe]"
    )
    rt.openai.describe_image.assert_awaited_once()
    # The bytes we sniffed decide the media type we declare.
    assert rt.openai.describe_image.await_args.kwargs["media_type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_a_cache_hit_costs_no_vision_call():
    """Stickers repeat constantly; the cache is most of the cost control."""
    rt = _vision_rt(cached="a smug cartoon frog")
    assert await vision.describe(rt, _photo_msg()) == (
        "[photo: a smug cartoon frog]"
    )
    rt.openai.describe_image.assert_not_awaited()
    rt.bot.get_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_fresh_description_is_cached():
    rt = _vision_rt()
    await vision.describe(rt, _photo_msg())
    written = rt.db.execute.await_args.args
    assert "media_descriptions" in written[0]
    assert written[1] == "u1"                       # keyed by file_unique_id


@pytest.mark.asyncio
async def test_a_provider_failure_still_reports_that_a_photo_arrived():
    """Blind is not deaf: the bot must still know something was posted."""
    rt = _vision_rt(described=None)
    assert await vision.describe(rt, _photo_msg()) == "[a photo]"


@pytest.mark.asyncio
async def test_a_download_explosion_is_survivable():
    rt = _vision_rt()
    rt.bot.get_file = AsyncMock(side_effect=RuntimeError("telegram said no"))
    assert await vision.describe(rt, _photo_msg()) == "[a photo]"


@pytest.mark.asyncio
async def test_an_oversized_file_is_refused_before_the_provider_sees_it():
    rt = _vision_rt()
    rt.bot.get_file = AsyncMock(return_value=SimpleNamespace(
        file_path="p/f.jpg", file_size=99 * 1024 * 1024,
    ))
    assert await vision.describe(rt, _photo_msg()) == "[a photo]"
    rt.openai.describe_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_non_image_payload_is_never_sent_to_the_provider():
    """An animated sticker's bytes are gzipped JSON. Sending them would
    burn a call and earn a 400."""
    async def _download_tgs(path, destination):
        destination.write(TGS)

    rt = _vision_rt()
    rt.bot.download_file = AsyncMock(side_effect=_download_tgs)
    assert await vision.describe(rt, _photo_msg()) == "[a photo]"
    rt.openai.describe_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_broken_cache_read_does_not_stop_the_description():
    rt = _vision_rt()
    rt.db.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))
    assert await vision.describe(rt, _photo_msg()) == (
        "[photo: a man in a bathrobe]"
    )


@pytest.mark.asyncio
async def test_no_media_no_note():
    assert await vision.describe(_vision_rt(), _blank_msg()) is None


# ── the handler: a description is context, never user input ──────────────────

def _handler(rt):
    router = chat.build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == "on_message")


def _seeing_rt(monkeypatch, *, seen="[photo: a cat in a hat]", policy="always"):
    rt = _rt_with()
    cfg = rt.chats.get_config.return_value
    cfg.vision_enabled = True
    cfg.response_policy = policy
    cfg.memory_enabled = True
    # The capability brief reads every scheduled-post switch off the config.
    for field in ("monthly_recap_enabled", "share_photo_enabled",
                  "comic_enabled", "fortune_enabled", "ether_enabled"):
        setattr(cfg, field, False)
    rt.memory = SimpleNamespace(record_message=AsyncMock())
    rt.persona_state = SimpleNamespace(
        current=AsyncMock(return_value=None), to_system_prompt=lambda s: "",
    )
    rt.openai = SimpleNamespace(chat=AsyncMock(return_value="sh-sha"))
    monkeypatch.setattr(vision, "describe", AsyncMock(return_value=seen))
    monkeypatch.setattr(chat, "_REACT_PROBABILITY", 0.0)
    monkeypatch.setattr(chat, "_DALE_GIF_PROBABILITY", 0.0)
    monkeypatch.setattr(chat, "maybe_summarize", AsyncMock())
    monkeypatch.setattr(chat, "resolve_impersonation", AsyncMock(return_value=None))
    return rt


def _media_msg(**kw):
    msg = _msg(**kw)
    msg.photo = [SimpleNamespace(file_id="big", file_unique_id="u1",
                                 file_size=90_000)]
    return msg


@pytest.mark.asyncio
async def test_a_captionless_photo_is_no_longer_dropped(monkeypatch):
    """Before vision, a bare photo had no text and fell out of the handler
    entirely — the bot never knew it happened."""
    rt = _seeing_rt(monkeypatch)
    captured = {}

    async def fake_build(**kwargs):
        captured.update(kwargs)
        from ipedro.memory.context_builder import BuiltContext
        return BuiltContext(messages=[{"role": "user", "content": "x"}], tokens=1)

    monkeypatch.setattr(chat, "build_context", fake_build)
    msg = _media_msg(text=None)
    msg.text = None
    msg.caption = None
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))

    await _handler(rt)(msg)

    assert captured["latest_user_text"] == "[photo: a cat in a hat]"
    rt.openai.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_description_is_written_to_memory_above_the_caption(
    monkeypatch,
):
    """One row, description first, so months later the summarizer and a
    semantic hit both still see what the picture was."""
    rt = _seeing_rt(monkeypatch)
    monkeypatch.setattr(chat, "should_respond", lambda *a, **k: False)
    msg = _media_msg(text=None)
    msg.text = None
    msg.caption = "look at this idiot"

    await _handler(rt)(msg)

    recorded = [c for c in rt.memory.record_message.await_args_list
                if c.kwargs.get("role") == "user"]
    assert len(recorded) == 1
    assert recorded[0].kwargs["content"] == (
        "[photo: a cat in a hat]\nlook at this idiot"
    )


@pytest.mark.asyncio
async def test_a_description_never_fires_a_canned_automod_bit(monkeypatch):
    """The sharpest one. If the bot's own words about a picture were
    treated as user input, a photo the model calls 'based' would trigger
    the automod table — the bot answering itself."""
    rt = _seeing_rt(monkeypatch, seen="[photo: a poster reading BASED]")
    monkeypatch.setattr(chat, "should_respond", lambda *a, **k: False)
    fired = []
    monkeypatch.setattr(chat, "_automod_response",
                        lambda t: fired.append(t) or None)
    msg = _media_msg(text=None)
    msg.text = None
    msg.caption = "heh"

    await _handler(rt)(msg)

    assert fired == ["heh"]          # matched on what the human typed, only


@pytest.mark.asyncio
async def test_the_switch_turns_looking_off(monkeypatch):
    rt = _seeing_rt(monkeypatch)
    rt.chats.get_config.return_value.vision_enabled = False
    msg = _media_msg(text=None)
    msg.text = None
    msg.caption = None

    await _handler(rt)(msg)

    vision.describe.assert_not_awaited()
    rt.openai.chat.assert_not_awaited()      # nothing to say about it


@pytest.mark.asyncio
async def test_a_commands_only_chat_never_pays_for_vision(monkeypatch):
    rt = _seeing_rt(monkeypatch, policy="commands")
    msg = _media_msg(text=None)
    msg.text = None
    msg.caption = None

    await _handler(rt)(msg)

    vision.describe.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_replied_to_picture_is_described_for_that_turn(monkeypatch):
    """'dale what is this' puts the photo on the replied-to message."""
    rt = _seeing_rt(monkeypatch, seen="[photo: a propane tank]")
    captured = {}

    async def fake_build(**kwargs):
        captured.update(kwargs)
        from ipedro.memory.context_builder import BuiltContext
        return BuiltContext(messages=[{"role": "user", "content": "x"}], tokens=1)

    monkeypatch.setattr(chat, "build_context", fake_build)
    reply_to = _media_msg(text=None)
    reply_to.from_user = SimpleNamespace(id=99, is_bot=False, username="o",
                                         first_name="O", last_name=None)
    msg = _msg(text="dale what is this")
    msg.reply_to_message = reply_to
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))

    await _handler(rt)(msg)

    assert "[photo: a propane tank]" in (captured["extra_system"] or "")


# ── the cache table must stay global ─────────────────────────────────────────

def test_media_descriptions_has_no_chat_id_column():
    """chat_migration re-keys every table carrying a chat_id. What's in a
    picture doesn't change when a group is upgraded to a supergroup, and
    the whole point of the cache is that one file is described once for
    every chat — so it must stay out of that."""
    from pathlib import Path
    sql = Path("ipedro/db/schema.sql").read_text()
    block = sql[sql.index("CREATE TABLE IF NOT EXISTS media_descriptions"):]
    assert "chat_id" not in block[:block.index(");")]


def test_the_vision_switch_exists_as_a_column():
    """ChatConfig.vision_enabled reads row['vision_enabled'] with no
    default, so a missing column is a hard boot failure, not a silent
    fallback."""
    from pathlib import Path
    sql = Path("ipedro/db/schema.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS vision_enabled" in sql


def test_photo_falls_back_when_the_biggest_size_is_too_large():
    """Detail matters, but not more than getting an answer at all: an
    oversized top size means step down, not give up."""
    small = SimpleNamespace(file_id="small", file_unique_id="us",
                            file_size=90_000)
    huge = SimpleNamespace(file_id="huge", file_unique_id="uh",
                           file_size=40_000_000)
    media = vision.extract_media(_blank_msg(photo=[small, huge]))
    assert media.file_id == "small"


def test_photo_with_unknown_sizes_still_takes_the_largest():
    small = SimpleNamespace(file_id="small", file_unique_id="us", file_size=None)
    big = SimpleNamespace(file_id="big", file_unique_id="ub", file_size=None)
    assert vision.extract_media(_blank_msg(photo=[small, big])).file_id == "big"
