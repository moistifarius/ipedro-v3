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
    clean_comment_text,
    extract_comment_media,
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
    c = pick_top_comment(listing)
    assert c["body"] == "actual funny take" and c["author"] == "realuser"


def test_pick_top_comment_skips_automod_and_deleted_and_long():
    listing = {"data": {"children": [
        _t1(body="beep boop I am automod", author="AutoModerator"),
        _t1(body="[deleted]", author="[deleted]"),
        _t1(body="x" * 5000, author="rambler"),          # too long, no media
        _t1(body="finally a good one", author="hero"),
    ]}}
    c = pick_top_comment(listing)
    assert c["body"] == "finally a good one" and c["author"] == "hero"


def test_pick_top_comment_none_when_all_unusable():
    listing = {"data": {"children": [_t1(body="[removed]", author="x")]}}
    assert pick_top_comment(listing) is None


def test_pick_top_comment_keeps_gif_only_comment():
    """A media-only comment (just a gif embed) is short and must be kept —
    it's exactly the case we want to render as a gif."""
    listing = {"data": {"children": [
        _t1(body="![gif](giphy|641arBi22PAty)", author="gifguy"),
    ]}}
    c = pick_top_comment(listing)
    assert c is not None and c["author"] == "gifguy"


# ───────────────────────── comment media ──────────────────────────────────
def test_clean_comment_text_strips_embeds():
    assert clean_comment_text("lol ![gif](giphy|abc123) so true") == "lol  so true"
    assert clean_comment_text("![img](xyz)") == ""
    assert clean_comment_text("just text") == "just text"


def test_extract_comment_media_from_giphy_metadata():
    comment = {
        "body": "![gif](giphy|641arBi22PAty)",
        "media_metadata": {
            "giphy|641arBi22PAty": {
                "status": "valid", "e": "AnimatedImage", "m": "image/gif",
                "s": {"gif": "https://i.giphy.com/media/641arBi22PAty/giphy.gif",
                      "mp4": "https://i.giphy.com/media/641arBi22PAty/giphy.mp4"},
            }
        },
    }
    m = extract_comment_media(comment)
    assert m is not None and m.kind == "animation"
    assert m.url.endswith(".mp4")   # mp4 preferred when present


def test_extract_comment_media_image_reply():
    comment = {
        "body": "![img](abc123)",
        "media_metadata": {
            "abc123": {"status": "valid", "e": "Image", "m": "image/png",
                       "s": {"u": "https://i.redd.it/abc123.png"}},
        },
    }
    m = extract_comment_media(comment)
    assert m is not None and m.kind == "photo" and m.url.endswith(".png")


def test_extract_comment_media_giphy_fallback_from_body():
    # No media_metadata — build the giphy URL from the id in the markdown.
    comment = {"body": "![gif](giphy|641arBi22PAty)"}
    m = extract_comment_media(comment)
    assert m is not None and m.kind == "animation"
    assert "641arBi22PAty" in m.url and m.url.endswith(".gif")


def test_extract_comment_media_none_for_plain_text():
    assert extract_comment_media({"body": "just a normal comment"}) is None


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


# ───────────────────── meme-request detection ─────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("give me a meme about cats", "cats"),
    ("hey pedro gimme a meme about the game", "the game"),
    ("find a meme about tax season please", "tax season"),
    ("post a meme of shrek", "shrek"),
    ("show me a meme on mondays", "mondays"),
    ("any memes about mondays?", "mondays"),
    ("drop a meme for the group", "the group"),
    ("send me a meme about crypto lol", "crypto"),
])
def test_detect_meme_request_explicit_topic(text, expected):
    from ipedro.reddit import detect_meme_request
    assert detect_meme_request(text) == expected


@pytest.mark.parametrize("text", [
    "give me a meme about this",
    "gimme a meme about this conversation",
    "find a meme about that",
    "give me a meme",                # no topic at all
    "meme this",
    "post a meme about whatever",
])
def test_detect_meme_request_deictic_means_derive(text):
    from ipedro.reddit import detect_meme_request
    assert detect_meme_request(text) == ""


@pytest.mark.parametrize("text", [
    "that meme about cats was funny",     # no request verb
    "I love memes",
    "the meme economy is wild",
    "what's the weather",
    "",
    None,
])
def test_detect_meme_request_ignores_non_requests(text):
    from ipedro.reddit import detect_meme_request
    assert detect_meme_request(text) is None


# ───────────────────── search url builders ────────────────────────────────
def test_search_url_multireddit_oauth_and_anon():
    from ipedro.reddit import _search_url
    subs = ("memes", "funny")
    assert _search_url(_OAUTH_BASE, subs, False) == \
        "https://oauth.reddit.com/r/memes+funny/search"
    assert _search_url(_ANON_BASE, subs, True) == \
        "https://www.reddit.com/r/memes+funny/search.json"


def test_sitewide_search_url():
    from ipedro.reddit import _sitewide_search_url
    assert _sitewide_search_url(_OAUTH_BASE, False) == \
        "https://oauth.reddit.com/search"
    assert _sitewide_search_url(_ANON_BASE, True) == \
        "https://www.reddit.com/search.json"


def test_choose_post_top_k_limits_pool():
    # With top_k=1 the first displayable candidate is always chosen.
    listing = {"data": {"children": [
        _t3(url="https://i.redd.it/first.jpg", title="first"),
        _t3(url="https://i.redd.it/second.jpg", title="second"),
        _t3(url="https://i.redd.it/third.jpg", title="third"),
    ]}}
    for seed in range(5):
        post = choose_post(listing, random.Random(seed), top_k=1)
        assert post["title"] == "first"


# ───────────────────── topic-subreddit discovery ──────────────────────────
def _t5(**data):
    return {"kind": "t5", "data": data}


def test_pick_topic_subreddits_filters_and_orders():
    from ipedro.reddit import pick_topic_subreddits
    listing = {"data": {"children": [
        _t5(display_name="u_someuser", subscribers=99999),        # profile
        _t5(display_name="nsfwsub", over18=True, subscribers=99999),
        _t5(display_name="tiny", subscribers=12),                 # too small
        _t5(display_name="privateclub", subreddit_type="private",
            subscribers=99999),
        _t5(display_name="lakers", subscribers=800000),           # ✓
        _t5(display_name="nba", subscribers=9000000),             # ✓
        _t5(display_name="basketball", subscribers=500000),       # over limit
    ]}}
    assert pick_topic_subreddits(listing, limit=2) == ["lakers", "nba"]


def test_subreddit_search_url():
    from ipedro.reddit import _subreddit_search_url
    assert _subreddit_search_url(_OAUTH_BASE, False) == \
        "https://oauth.reddit.com/subreddits/search"
    assert _subreddit_search_url(_ANON_BASE, True) == \
        "https://www.reddit.com/subreddits/search.json"


@pytest.mark.asyncio
async def test_candidates_from_topic_sub_falls_back_to_top_listing(monkeypatch):
    import ipedro.reddit as rd

    calls = []

    async def fake_api_context(cid, secret, ua):
        return rd._ANON_BASE, {"User-Agent": ua}, True

    async def fake_get_json(client, url, **params):
        calls.append((url, params))
        if "/search" in url:
            return None                                    # sub search dry
        if "/top" in url:
            return {"data": {"children": [
                # A plain news photo — must be filtered out of the fallback.
                _t3(url="https://i.redd.it/news.jpg", title="game recap",
                    subreddit="lakers", permalink="/r/lakers/comments/n/y/"),
                # A flaired meme — the only thing the fallback should keep.
                _t3(url="https://i.redd.it/top.jpg", title="community classic",
                    subreddit="lakers", permalink="/r/lakers/comments/z/x/",
                    link_flair_text="Meme"),
            ]}}
        return None

    monkeypatch.setattr(rd, "_api_context", fake_api_context)
    monkeypatch.setattr(rd, "_get_json", fake_get_json)

    posts = await rd.candidates_from_topic_sub("lakers")
    assert len(posts) == 1 and posts[0]["title"] == "community classic"
    # First tried the in-sub 'meme' search, then the top listing.
    assert "/r/lakers/search" in calls[0][0]
    assert calls[0][1]["q"] == "meme"
    assert "/r/lakers/top" in calls[1][0]


def test_is_meme_flavored_signals():
    from ipedro.reddit import is_meme_flavored
    # Known meme community.
    assert is_meme_flavored({"subreddit": "me_irl", "title": "x"})
    assert is_meme_flavored({"subreddit": "MEMES", "title": "x"})
    # Meme-ish flair inside a topic community.
    assert is_meme_flavored({"subreddit": "nba", "title": "big win",
                             "link_flair_text": "Shitpost"})
    assert is_meme_flavored({"subreddit": "nba", "title": "big win",
                             "link_flair_text": "Humour"})
    # 'meme' in the title.
    assert is_meme_flavored({"subreddit": "nba", "title": "playoff memes"})
    # A plain news photo is NOT meme-flavored.
    assert not is_meme_flavored({"subreddit": "nba",
                                 "title": "Lakers acquire star in trade",
                                 "link_flair_text": "News"})


# ─────────── detection hardening (adversarial-review regressions) ─────────
@pytest.mark.parametrize("text", [
    # casual mentions / narration must NOT hijack the reply
    "haha good meme this is perfect",
    "did you see the meme this morning?",
    "I'll send a meme about it later",
    "he said he'd give me a meme about cats tomorrow",
    "im going to post a meme about the election on twitter",
    "do you have some memes on your phone?",
    "I got a meme about this from Dani, so funny",
    "no i dont have a meme about that saved",
])
def test_detect_meme_request_rejects_casual_mentions(text):
    from ipedro.reddit import detect_meme_request
    assert detect_meme_request(text) is None


@pytest.mark.parametrize("text,expected", [
    # lead-ins that put the verb in request position
    ("can you find a meme about parking tickets", "parking tickets"),
    ("could you post a meme about mondays", "mondays"),
    ("sure, drop a meme about it", ""),           # deictic after comma
    # question forms addressed to the bot
    ("do you have a meme about mondays", "mondays"),
    ("got a meme about mondays?", "mondays"),
    # anchored 'meme this' with a name prefix
    ("pedro, meme this", ""),
    ("hey dale meme that", ""),
    # topic hygiene
    ("give me a meme about cats, man", "cats"),     # comma-vocative dropped
    ("give me a meme about the dude", "the dude"),  # real topic preserved
    ("gimme a meme about cats tomorrow", "cats"),   # time adverb stripped
])
def test_detect_meme_request_hardened_positives(text, expected):
    from ipedro.reddit import detect_meme_request
    assert detect_meme_request(text) == expected


@pytest.mark.parametrize("text,expected", [
    # casual name-first asks — every alias the bot answers to counts as
    # a request lead-in ("duder gimme a meme about that?")
    ("duder gimme a meme about that?", ""),
    ("pedro gimme a meme about that?", ""),
    ("dude, gimme a meme about that", ""),
    ("hey duder gimme a meme about the game", "the game"),
    ("boomhauer give me a meme about cats", "cats"),
    ("duderino post a meme about mondays", "mondays"),
    ("duder meme this", ""),
    ("el duderino, meme that", ""),
])
def test_detect_meme_request_all_bot_name_leadins(text, expected):
    from ipedro.reddit import detect_meme_request
    assert detect_meme_request(text) == expected


# ───────────── detection widening (natural request phrasings) ─────────────
@pytest.mark.parametrize("text,expected", [
    ("make me a meme about mondays", "mondays"),
    ("pedro make a meme about tax season", "tax season"),
    ("someone make a meme of this", ""),
    ("somebody post a meme about the game", "the game"),
    ("i want a meme about cats", "cats"),
    ("we need a meme of this", ""),
    ("can i get a meme about mondays", "mondays"),
    ("can we get a meme of shrek", "shrek"),
    ("lemme get a meme about crypto", "crypto"),
    ("meme about cats", "cats"),                 # bare, message start
    ("pedro meme about cats", "cats"),           # bare after bot name
    ("duder, meme of the lakers", "the lakers"),
    ("make this a meme", ""),
    ("turn that into a meme", ""),
    ("whip up a meme about winter", "winter"),
    ("hit me with a meme about mondays", "mondays"),
    ("throw me a meme about golf", "golf"),
])
def test_detect_meme_request_natural_phrasings(text, expected):
    from ipedro.reddit import detect_meme_request
    assert detect_meme_request(text) == expected


@pytest.mark.parametrize("text", [
    # narration with the new verbs must stay dead
    "he wants a meme about cats",
    "she said she needs a meme about that",
    "i was gonna make a meme about it yesterday",
])
def test_detect_meme_request_new_verbs_still_reject_narration(text):
    from ipedro.reddit import detect_meme_request
    assert detect_meme_request(text) is None


# ─────────────── meme GENERATION vs FETCH intent split ────────────────────
@pytest.mark.parametrize("text,topic", [
    ("make me a meme about mondays", "mondays"),
    ("pedro make a meme about tax season", "tax season"),
    ("create a meme about cats", "cats"),
    ("generate a meme of shrek", "shrek"),
    ("design a meme about cats", "cats"),
    ("whip up a meme about winter", "winter"),
    ("make this a meme", ""),
    ("pedro turn that into a meme", ""),
])
def test_is_meme_generation_request_true(text, topic):
    from ipedro.reddit import detect_meme_request, is_meme_generation_request
    assert detect_meme_request(text) == topic       # still a detected request
    assert is_meme_generation_request(text) is True


@pytest.mark.parametrize("text", [
    "find me a meme about mondays",
    "give me a meme about cats",
    "gimme a meme about that",
    "any memes about mondays?",
    "post a meme of shrek",
    "that meme about cats was funny",     # not a request → not generation
    "",
    None,
])
def test_is_meme_generation_request_false(text):
    from ipedro.reddit import is_meme_generation_request
    assert is_meme_generation_request(text) is False
