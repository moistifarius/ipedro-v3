"""Tests for the non-Reddit meme source parsers (Giphy / Imgur / KYM)."""

from __future__ import annotations

from ipedro.meme_sources import (
    parse_giphy_results,
    parse_imgur_results,
    parse_imgur_top_comment,
    parse_kym_meme_names,
)


# ────────────────────────────── giphy ──────────────────────────────────────
def test_parse_giphy_prefers_mp4_and_caps():
    payload = {"data": [
        {"title": "cat gif", "images": {"original": {
            "mp4": "https://media.giphy.com/a.mp4",
            "url": "https://media.giphy.com/a.gif"}}},
        {"title": "", "images": {"original": {
            "url": "https://media.giphy.com/b.gif"}}},
        {"title": "no media", "images": {}},                 # dropped
    ]}
    got = parse_giphy_results(payload, limit=5)
    assert len(got) == 2
    assert got[0]["media"].url.endswith(".mp4")              # mp4 preferred
    assert got[0]["media"].kind == "animation"
    assert got[1]["title"] == "(untitled gif)"
    assert all(c["source"] == "giphy" for c in got)


def test_parse_giphy_empty_payload():
    assert parse_giphy_results({}) == []
    assert parse_giphy_results({"data": []}) == []


# ────────────────────────────── imgur ──────────────────────────────────────
def test_parse_imgur_image_album_nsfw_and_animated():
    payload = {"data": [
        {"nsfw": True, "title": "nope",
         "link": "https://i.imgur.com/x.jpg", "id": "n1"},   # nsfw dropped
        {"nsfw": False, "is_album": True, "title": "album meme", "id": "a1",
         "images": [{"link": "https://i.imgur.com/first.png"}]},
        {"nsfw": False, "title": "moving meme", "id": "v1", "animated": True,
         "link": "https://i.imgur.com/v.gifv",
         "mp4": "https://i.imgur.com/v.mp4"},
        {"nsfw": False, "title": "gif meme", "id": "g1",
         "link": "https://i.imgur.com/g.gif"},
        {"nsfw": False, "title": "not media", "id": "t1",
         "link": "https://imgur.com/gallery/xyz"},           # no ext dropped
    ]}
    got = parse_imgur_results(payload, limit=6)
    kinds = [(c["title"], c["media"].kind) for c in got]
    assert kinds == [
        ("album meme", "photo"),
        ("moving meme", "video"),
        ("gif meme", "animation"),
    ]
    assert got[0]["imgur_id"] == "a1"
    assert all(c["source"] == "imgur" for c in got)


def test_parse_imgur_top_comment_skips_unusable():
    payload = {"data": [
        {"comment": "[deleted]"},
        {"comment": "x" * 2000},           # too long
        {"comment": "actual funny reply"},
    ]}
    assert parse_imgur_top_comment(payload) == "actual funny reply"
    assert parse_imgur_top_comment({"data": []}) is None


# ─────────────────────────── knowyourmeme ──────────────────────────────────
def test_parse_kym_names_extracts_slugs_in_order():
    html = (
        '<a href="/memes/popular">nav</a>'
        '<a href="/memes/distracted-boyfriend">entry</a>'
        '<a href="/memes/distracted-boyfriend">dupe</a>'
        '<a href="/memes/this-is-fine">entry</a>'
        '<a href="/memes/woman-yelling-at-a-cat">entry</a>'
    )
    assert parse_kym_meme_names(html, limit=2) == [
        "distracted boyfriend", "this is fine",
    ]


def test_parse_kym_names_empty_or_garbage():
    assert parse_kym_meme_names("") == []
    assert parse_kym_meme_names("<html>nothing here</html>") == []
    # Nav/section slugs alone yield nothing.
    assert parse_kym_meme_names('<a href="/memes/trending">x</a>') == []
