"""Tests for the Reddit meme puller's pure parsing helpers."""

from __future__ import annotations

import random

import pytest

from ipedro.reddit import (
    _ANON_BASE,
    _OAUTH_BASE,
    Meme,
    Media,
    _api_context,
    _comments_url,
    _listing_url,
    build_caption,
    choose_post,
    pick_top_comment,
    reddit_audio_candidates,
    reset_token_cache,
    resolve_media,
)


# ───────────────────────────── media resolution ───────────────────────────
def test_resolve_direct_image():
    m = resolve_media({"url_overridden_by_dest": "https://i.redd.it/abc.jpg"})
    assert m is not None and m.kind == "photo"
    assert m.url.endswith("abc.jpg")


def test_resolve_gif_is_animation():
    m = resolve_media({"url": "https://i.redd.it/x.gif"})
    assert m.kind == "animation"


def test_resolve_imgur_gifv_becomes_mp4_video():
    m = resolve_media({"url": "https://i.imgur.com/x.gifv"})
    assert m.kind == "video" and m.url.endswith("x.mp4")


def test_resolve_reddit_video_with_audio():
    post = {
        "is_video": True,
        "media": {"reddit_video": {
            "fallback_url": "https://v.redd.it/abc123/DASH_720.mp4?source=fallback",
            "has_audio": True,
        }},
    }
    m = resolve_media(post)
    assert m.kind == "video"
    assert m.url == "https://v.redd.it/abc123/DASH_720.mp4"   # query stripped
    assert m.audio_candidates and all("abc123" in a for a in m.audio_candidates)


def test_resolve_reddit_video_without_audio_has_no_candidates():
    post = {
        "is_video": True,
        "media": {"reddit_video": {
            "fallback_url": "https://v.redd.it/z/DASH_480.mp4",
            "has_audio": False,
        }},
    }
    m = resolve_media(post)
    assert m.kind == "video" and m.audio_candidates == []


def test_resolve_gallery_first_item():
    post = {
        "is_gallery": True,
        "gallery_data": {"items": [{"media_id": "img1"}, {"media_id": "img2"}]},
        "media_metadata": {
            "img1": {"status": "valid", "m": "image/png",
                     "s": {"u": "https://i.redd.it/img1.png"}},
            "img2": {"status": "valid", "m": "image/png",
                     "s": {"u": "https://i.redd.it/img2.png"}},
        },
    }
    m = resolve_media(post)
    assert m.kind == "photo" and "img1" in m.url


def test_resolve_nsfw_is_skipped():
    assert resolve_media({"over_18": True,
                          "url": "https://i.redd.it/x.jpg"}) is None


def test_resolve_text_post_is_none():
    assert resolve_media({"is_self": True, "url": "https://reddit.com/r/x"}) is None


def test_resolve_preview_fallback_for_image_hint():
    post = {
        "post_hint": "image",
        "url": "https://example.com/page",   # not a direct image
        "preview": {"images": [{"source": {"u": "https://i.redd.it/p.jpg",
                                           "url": "https://i.redd.it/p.jpg"}}]},
    }
    m = resolve_media(post)
    assert m is not None and m.kind == "photo"


# ───────────────────────────── audio candidates ───────────────────────────
def test_audio_candidates_derive_from_base():
    cands = reddit_audio_candidates("https://v.redd.it/abc/DASH_1080.mp4?x=1")
    assert cands == [
        "https://v.redd.it/abc/DASH_AUDIO_128.mp4",
        "https://v.redd.it/abc/DASH_AUDIO_64.mp4",
        "https://v.redd.it/abc/DASH_audio.mp4",
    ]


# ───────────────────────────── choose_post ────────────────────────────────
def _t3(**data):
    return {"kind": "t3", "data": data}


def test_choose_post_skips_undisplayable_and_nsfw_and_stickied():
    listing = {"data": {"children": [
        _t3(stickied=True, url="https://i.redd.it/a.jpg"),   # stickied
        _t3(over_18=True, url="https://i.redd.it/b.jpg"),    # nsfw
        _t3(is_self=True, url="https://reddit.com/x"),       # text
        _t3(url="https://example.com/notmedia"),             # no media
        _t3(url="https://i.redd.it/good.jpg", title="ok"),   # ✓ only valid
    ]}}
    post = choose_post(listing, random.Random(0))
    assert post is not None and post["title"] == "ok"


def test_choose_post_none_when_no_media():
    listing = {"data": {"children": [_t3(is_self=True, url="x")]}}
    assert choose_post(listing, random.Random(0)) is None


# ───────────────────────────── comments ───────────────────────────────────
def _t1(**data):
    return {"kind": "t1", "data": data}


def test_pick_top_comment_returns_first_usable():
    listing = {"data": {"children": [
        _t1(stickied=True, body="pinned rules", author="mod"),
        _t1(body="[removed]", author="ghost"),
        _t1(body="actual funny take", author="realuser"),
        _t1(body="second best", author="other"),
    ]}}
    body, author = pick_top_comment(listing)
    assert body == "actual funny take" and author == "realuser"


def test_pick_top_comment_skips_automod_and_deleted_and_long():
    listing = {"data": {"children": [
        _t1(body="beep boop I am automod", author="AutoModerator"),
        _t1(body="[deleted]", author="[deleted]"),
        _t1(body="x" * 5000, author="rambler"),          # too long
        _t1(body="finally a good one", author="hero"),
    ]}}
    body, author = pick_top_comment(listing)
    assert body == "finally a good one" and author == "hero"


def test_pick_top_comment_none_when_all_unusable():
    listing = {"data": {"children": [_t1(body="[removed]", author="x")]}}
    assert pick_top_comment(listing) == (None, None)


# ───────────────────────────── caption ────────────────────────────────────
def test_build_caption_uses_comment_and_footer():
    meme = Meme(subreddit="me_irl", title="t", post_author="op",
                permalink="/x", media=Media("photo", "u"),
                comment="this is the top comment", comment_author="u1")
    cap = build_caption(meme)
    assert "this is the top comment" in cap
    assert "r/me_irl" in cap


def test_build_caption_falls_back_to_title():
    meme = Meme(subreddit="memes", title="the title", post_author="op",
                permalink="/x", media=Media("photo", "u"), comment=None)
    assert "the title" in build_caption(meme)


def test_build_caption_truncates_to_limit():
    meme = Meme(subreddit="funny", title="t", post_author="op",
                permalink="/x", media=Media("photo", "u"),
                comment="x" * 5000)
    cap = build_caption(meme, limit=1024)
    assert len(cap) <= 1024
    assert cap.endswith("· r/funny")
    assert "…" in cap


# ───────────────────────────── url builders ───────────────────────────────
def test_listing_url_oauth_has_no_json_suffix():
    assert _listing_url(_OAUTH_BASE, "memes", False) == \
        "https://oauth.reddit.com/r/memes/top"


def test_listing_url_anonymous_has_json_suffix():
    assert _listing_url(_ANON_BASE, "memes", True) == \
        "https://www.reddit.com/r/memes/top.json"


def test_comments_url_oauth_keeps_permalink_no_suffix():
    url = _comments_url(_OAUTH_BASE, "/r/memes/comments/abc/slug/", False)
    assert url == "https://oauth.reddit.com/r/memes/comments/abc/slug/"


def test_comments_url_anonymous_strips_slash_and_adds_json():
    url = _comments_url(_ANON_BASE, "/r/memes/comments/abc/slug/", True)
    assert url == "https://www.reddit.com/r/memes/comments/abc/slug.json"


# ───────────────────────── api context / token ────────────────────────────
@pytest.mark.asyncio
async def test_api_context_anonymous_without_credentials():
    reset_token_cache()
    # No network hit when creds absent — resolves straight to anon.
    base, headers, json_suffix = await _api_context("", "", "ua/1.0")
    assert base == _ANON_BASE
    assert json_suffix is True
    assert "Authorization" not in headers
    assert headers["User-Agent"] == "ua/1.0"


@pytest.mark.asyncio
async def test_api_context_uses_cached_token_for_oauth(monkeypatch):
    """With creds + a (mocked) valid cached token, we hit the OAuth host
    with a bearer header and no .json suffix."""
    import ipedro.reddit as rd

    async def _fake_token(cid, secret, ua):
        return "TOK123"

    monkeypatch.setattr(rd, "_get_oauth_token", _fake_token)
    base, headers, json_suffix = await _api_context("id", "secret", "ua/1.0")
    assert base == _OAUTH_BASE
    assert json_suffix is False
    assert headers["Authorization"] == "bearer TOK123"


@pytest.mark.asyncio
async def test_api_context_falls_back_to_anon_when_token_fails(monkeypatch):
    import ipedro.reddit as rd

    async def _no_token(cid, secret, ua):
        return None

    monkeypatch.setattr(rd, "_get_oauth_token", _no_token)
    base, headers, json_suffix = await _api_context("id", "secret", "ua/1.0")
    assert base == _ANON_BASE
    assert json_suffix is True
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_token_failure_backs_off_then_reset_clears_it(monkeypatch):
    """A failed mint sets a retry cooldown so we don't hammer Reddit; a
    successful mint is cached and reused; reset clears the back-off."""
    import ipedro.reddit as rd

    reset_token_cache()
    calls = {"n": 0}

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("boom", request=None, response=None)

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            calls["n"] += 1
            # First attempt fails (bad creds), later attempts would succeed.
            if calls["n"] == 1:
                return _Resp(401, {})
            return _Resp(200, {"access_token": "TOK", "expires_in": 3600})

    monkeypatch.setattr(rd.httpx, "AsyncClient", _FakeClient)

    # 1st call fails → None, and a back-off is armed.
    assert await rd._get_oauth_token("id", "sec", "ua") is None
    assert calls["n"] == 1
    # 2nd call is inside the cooldown → returns None WITHOUT another POST.
    assert await rd._get_oauth_token("id", "sec", "ua") is None
    assert calls["n"] == 1
    # Clearing the back-off allows a fresh attempt, which now succeeds.
    reset_token_cache()
    assert await rd._get_oauth_token("id", "sec", "ua") == "TOK"
    assert calls["n"] == 2
    # Subsequent call is served from cache (no new POST).
    assert await rd._get_oauth_token("id", "sec", "ua") == "TOK"
    assert calls["n"] == 2
    reset_token_cache()
