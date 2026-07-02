"""Tests for the topic-meme pipeline (query parsing, candidate gathering
order, and the AI judge's pick semantics)."""

from __future__ import annotations

import pytest

import ipedro.meme_finder as mf
from ipedro.meme_finder import (
    find_relevant_meme,
    gather_candidates,
    parse_pick,
    parse_topic_queries,
)
from ipedro.reddit import Media, Meme


# ───────────────────────── parse_topic_queries ─────────────────────────────
def test_parse_topic_queries_strips_decorations():
    raw = '1. lakers game tonight\n- basketball\n"nba playoffs"\n'
    assert parse_topic_queries(raw) == [
        "lakers game tonight", "basketball", "nba playoffs",
    ]


def test_parse_topic_queries_caps_dedupes_and_drops_runaways():
    raw = (
        "cats\n"
        "CATS\n"                                  # dupe (case-insensitive)
        "this line is way too long to be a reasonable search query at all\n"
        "dogs\n"
        "birds\n"                                  # over the cap of 3
        "fish\n"
    )
    assert parse_topic_queries(raw) == ["cats", "dogs", "birds"]


def test_parse_topic_queries_empty_input():
    assert parse_topic_queries(None) == []
    assert parse_topic_queries("") == []
    assert parse_topic_queries("\n\n") == []


# ───────────────────────────── parse_pick ───────────────────────────────────
@pytest.mark.parametrize("text,n,expected", [
    ("3", 5, 3),
    ("0", 5, 0),
    ("#2", 5, 2),
    ("option 1.", 5, 1),
    ("7", 5, None),          # out of range
    ("none of these", 5, None),
    ("", 5, None),
    (None, 5, None),
])
def test_parse_pick(text, n, expected):
    assert parse_pick(text, n) == expected


# ───────────────────────── fakes for orchestration ─────────────────────────
def _post(permalink, title, sub="memes"):
    return {"permalink": permalink, "title": title, "subreddit": sub,
            "url": f"https://i.redd.it{permalink}.jpg"}


class _FakeAI:
    def __init__(self, answer):
        self.answer = answer
        self.prompts: list[str] = []

    async def cheap_completion(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.answer


@pytest.fixture
def wired(monkeypatch):
    """Monkeypatch meme_finder's source dependencies with recording fakes.

    Giphy/Imgur stay inert by default (no keys); KYM returns no expansion
    names unless a test sets state['kym'].
    """
    calls = {"topic_subs": [], "sub_candidates": [], "searches": [],
             "meme_for": [], "kym": [], "giphy": [], "imgur": [],
             "imgur_comments": []}
    state = {
        "topic_subs": ["lakers"],
        "sub_posts": [_post("/r/lakers/1", "lebron meme", "lakers")],
        "search_posts": {
            "default": [_post("/r/memes/2", "generic meme")],
        },
        "kym": [],
        "giphy_posts": [],
        "imgur_posts": [],
        "imgur_comment": "imgur top comment",
    }

    async def fake_topic_subs(query, **kw):
        calls["topic_subs"].append(query)
        return state["topic_subs"]

    async def fake_sub_candidates(sub, **kw):
        calls["sub_candidates"].append(sub)
        return state["sub_posts"]

    async def fake_search(query, **kw):
        calls["searches"].append(query)
        return state["search_posts"].get(query,
                                         state["search_posts"]["default"])

    async def fake_meme_for_post(post, **kw):
        calls["meme_for"].append(post)
        return Meme(subreddit=post["subreddit"], title=post["title"],
                    post_author="op", permalink=post["permalink"],
                    media=Media("photo", post["url"]))

    async def fake_kym(query, **kw):
        calls["kym"].append(query)
        return state["kym"]

    async def fake_giphy(query, api_key, **kw):
        calls["giphy"].append(query)
        return state["giphy_posts"]

    async def fake_imgur(query, client_id, **kw):
        calls["imgur"].append(query)
        return state["imgur_posts"]

    async def fake_imgur_comment(gallery_id, client_id, **kw):
        calls["imgur_comments"].append(gallery_id)
        return state["imgur_comment"]

    monkeypatch.setattr(mf, "search_topic_subreddits", fake_topic_subs)
    monkeypatch.setattr(mf, "candidates_from_topic_sub", fake_sub_candidates)
    monkeypatch.setattr(mf, "search_meme_candidates", fake_search)
    monkeypatch.setattr(mf, "meme_for_post", fake_meme_for_post)
    monkeypatch.setattr(mf, "kym_meme_names", fake_kym)
    monkeypatch.setattr(mf, "giphy_candidates", fake_giphy)
    monkeypatch.setattr(mf, "imgur_candidates", fake_imgur)
    monkeypatch.setattr(mf, "imgur_top_comment", fake_imgur_comment)
    return calls, state


# ───────────────────────── gather_candidates ───────────────────────────────
@pytest.mark.asyncio
async def test_gather_candidates_topic_sub_first_and_dedupes(wired):
    calls, state = wired
    # Same permalink appears in both sources — kept once, topic-sub first.
    state["search_posts"]["default"] = [
        _post("/r/lakers/1", "lebron meme", "lakers"),   # dupe
        _post("/r/memes/9", "other meme"),
    ]
    got = await gather_candidates(["lakers game", "basketball"])
    permalinks = [p["permalink"] for p in got]
    assert permalinks[0] == "/r/lakers/1"           # topic community first
    assert permalinks.count("/r/lakers/1") == 1     # deduped
    assert "/r/memes/9" in permalinks
    # Discovery used the PRIMARY (most specific) query only.
    assert calls["topic_subs"] == ["lakers game"]
    # Both queries fed the meme-sub/sitewide search layer.
    assert calls["searches"] == ["lakers game", "basketball"]


@pytest.mark.asyncio
async def test_gather_candidates_survives_discovery_failure(wired, monkeypatch):
    calls, state = wired

    async def boom(query, **kw):
        raise RuntimeError("reddit down")

    monkeypatch.setattr(mf, "search_topic_subreddits", boom)
    got = await gather_candidates(["anything"])
    # Falls through to the search layer instead of crashing.
    assert got and got[0]["permalink"] == "/r/memes/2"


# ───────────────────────── find_relevant_meme ──────────────────────────────
@pytest.mark.asyncio
async def test_judge_pick_is_used(wired):
    calls, state = wired
    state["topic_subs"] = []
    state["search_posts"]["default"] = [
        _post("/r/memes/1", "off topic"),
        _post("/r/memes/2", "dead on topic"),
    ]
    ai = _FakeAI("2")
    meme = await find_relevant_meme(ai, ["cats"], topic_label="cats")
    assert meme is not None and meme.permalink == "/r/memes/2"
    # The judge saw numbered candidates with their subreddits.
    assert "1. r/memes" in ai.prompts[-1]


@pytest.mark.asyncio
async def test_judge_zero_is_final(wired):
    """Judge saying 'none of these are a relevant meme' means NO post —
    posting a news photo that matched the query is the failure mode users
    notice, so an honest miss always wins."""
    calls, _ = wired
    ai = _FakeAI("0")
    meme = await find_relevant_meme(ai, ["cats"], topic_label="cats")
    assert meme is None
    assert calls["meme_for"] == []      # nothing fetched


@pytest.mark.asyncio
async def test_unusable_judge_falls_back_to_first(wired):
    calls, _ = wired
    ai = _FakeAI("i cannot decide, they are all beautiful")
    meme = await find_relevant_meme(ai, ["cats"], topic_label="cats")
    assert meme is not None                       # search order trusted
    assert calls["meme_for"][0]["permalink"] == "/r/lakers/1"


@pytest.mark.asyncio
async def test_no_candidates_returns_none_without_judging(wired):
    calls, state = wired
    state["topic_subs"] = []
    state["sub_posts"] = []
    state["search_posts"]["default"] = []
    ai = _FakeAI("1")
    meme = await find_relevant_meme(ai, ["cats"], topic_label="cats")
    assert meme is None
    assert ai.prompts == []             # judge never consulted


@pytest.mark.asyncio
async def test_empty_queries_short_circuit(wired):
    calls, _ = wired
    ai = _FakeAI("1")
    assert await find_relevant_meme(ai, [], topic_label="x") is None
    assert await find_relevant_meme(ai, ["", "  "], topic_label="x") is None
    assert calls["topic_subs"] == [] and calls["searches"] == []


@pytest.mark.asyncio
async def test_non_meme_candidates_sink_below_memes(wired):
    """A news photo from the sitewide sweep must sort BELOW meme-flavored
    posts, so the judge-unavailable fallback (first candidate) is a meme."""
    calls, state = wired
    state["topic_subs"] = []
    state["search_posts"]["default"] = [
        {"permalink": "/r/nba/1", "title": "Lakers acquire star in trade",
         "subreddit": "nba", "url": "https://i.redd.it/news.jpg"},   # news
        {"permalink": "/r/nba/2", "title": "playoff meme goes hard",
         "subreddit": "nba", "url": "https://i.redd.it/m.jpg"},      # meme title
        {"permalink": "/r/nba/3", "title": "big win last night",
         "subreddit": "nba", "url": "https://i.redd.it/w.jpg",
         "link_flair_text": "Meme"},                                 # meme flair
    ]
    got = await gather_candidates(["lakers"])
    permalinks = [p["permalink"] for p in got]
    # Meme-flavored (2: title, 3: flair) come first, news photo sinks last.
    assert permalinks.index("/r/nba/1") == len(permalinks) - 1
    # Judge-unavailable → first candidate → a meme, not the news photo.
    ai = _FakeAI("no clue")
    meme = await find_relevant_meme(ai, ["lakers"], topic_label="lakers")
    assert calls["meme_for"][0]["permalink"] != "/r/nba/1"


@pytest.mark.asyncio
async def test_judge_sees_flair_tags(wired):
    calls, state = wired
    state["topic_subs"] = []
    state["search_posts"]["default"] = [
        {"permalink": "/r/nba/3", "title": "big win",
         "subreddit": "nba", "url": "https://i.redd.it/w.jpg",
         "link_flair_text": "Meme"},
    ]
    ai = _FakeAI("1")
    await find_relevant_meme(ai, ["lakers"], topic_label="lakers")
    assert "[Meme]" in ai.prompts[-1]


# ───────────────────── multi-source + KYM expansion ────────────────────────
@pytest.mark.asyncio
async def test_kym_names_expand_the_query_list(wired):
    calls, state = wired
    state["kym"] = ["distracted boyfriend", "lakers"]   # 'lakers' is a dupe
    await gather_candidates(["lakers"])
    # Expansion queried KYM with the primary query only…
    assert calls["kym"] == ["lakers"]
    # …and the reddit search ran for the original + the NEW name only.
    assert calls["searches"] == ["lakers", "distracted boyfriend"]


@pytest.mark.asyncio
async def test_giphy_imgur_only_run_with_keys(wired):
    calls, state = wired
    await gather_candidates(["cats"])
    assert calls["giphy"] == [] and calls["imgur"] == []
    await gather_candidates(["cats"], giphy_api_key="G", imgur_client_id="I")
    assert calls["giphy"] == ["cats"] and calls["imgur"] == ["cats"]


def _giphy_candidate(url="https://media.giphy.com/x.mp4", title="cat gif"):
    return {"source": "giphy", "title": title, "media": Media("animation", url)}


def _imgur_candidate(link="https://i.imgur.com/y.jpg", title="cat pic",
                     imgur_id="abc"):
    return {"source": "imgur", "title": title,
            "media": Media("photo", link), "imgur_id": imgur_id}


@pytest.mark.asyncio
async def test_giphy_winner_builds_giphy_meme(wired):
    calls, state = wired
    state["topic_subs"] = []
    state["search_posts"]["default"] = []
    state["giphy_posts"] = [_giphy_candidate()]
    ai = _FakeAI("1")
    meme = await find_relevant_meme(
        ai, ["cats"], topic_label="cats", giphy_api_key="G",
    )
    assert meme is not None and meme.source == "giphy"
    assert meme.media.kind == "animation"
    # No reddit fetch for a giphy winner.
    assert calls["meme_for"] == []
    # Caption footer says the source, not r/<something>.
    from ipedro.reddit import build_caption
    assert build_caption(meme).endswith("· giphy")


@pytest.mark.asyncio
async def test_imgur_winner_pulls_top_comment(wired):
    calls, state = wired
    state["topic_subs"] = []
    state["search_posts"]["default"] = []
    state["imgur_posts"] = [_imgur_candidate(imgur_id="zzz")]
    ai = _FakeAI("1")
    meme = await find_relevant_meme(
        ai, ["cats"], topic_label="cats", imgur_client_id="I",
    )
    assert meme is not None and meme.source == "imgur"
    assert meme.comment == "imgur top comment"
    assert calls["imgur_comments"] == ["zzz"]


@pytest.mark.asyncio
async def test_judge_sees_source_labels(wired):
    calls, state = wired
    state["topic_subs"] = []
    state["search_posts"]["default"] = [_post("/r/memes/2", "reddit meme")]
    state["giphy_posts"] = [_giphy_candidate(title="giphy gif")]
    ai = _FakeAI("1")
    await find_relevant_meme(
        ai, ["cats"], topic_label="cats", giphy_api_key="G",
    )
    prompt = ai.prompts[-1]
    assert "r/memes" in prompt and "giphy — giphy gif" in prompt


# ───────────────────────── vote-aware ranking ──────────────────────────────
def test_candidate_score_and_format():
    from ipedro.meme_finder import _fmt_score, candidate_score
    assert candidate_score({"score": 12345}) == 12345
    assert candidate_score({"ups": 88}) == 88
    assert candidate_score({"title": "no votes"}) is None
    assert _fmt_score(12345) == " (12.3k↑)"
    assert _fmt_score(2000) == " (2k↑)"
    assert _fmt_score(88) == " (88↑)"
    assert _fmt_score(None) == ""


@pytest.mark.asyncio
async def test_candidates_ranked_by_votes_within_meme_tier(wired):
    calls, state = wired
    state["topic_subs"] = []
    state["search_posts"]["default"] = [
        dict(_post("/r/memes/low", "barely upvoted meme"), score=40),
        dict(_post("/r/memes/high", "banger meme"), score=90000),
        dict(_post("/r/memes/mid", "decent meme"), score=1200),
    ]
    got = await gather_candidates(["cats"])
    assert [p["permalink"] for p in got] == [
        "/r/memes/high", "/r/memes/mid", "/r/memes/low",
    ]


@pytest.mark.asyncio
async def test_low_vote_junk_pruned_when_enough_remain(wired):
    calls, state = wired
    state["topic_subs"] = []
    state["search_posts"]["default"] = [
        dict(_post(f"/r/memes/{i}", f"meme {i}"), score=1000 + i)
        for i in range(4)
    ] + [dict(_post("/r/memes/junk", "junk meme"), score=3)]
    got = await gather_candidates(["cats"])
    assert all(p["permalink"] != "/r/memes/junk" for p in got)


@pytest.mark.asyncio
async def test_low_vote_kept_when_candidates_scarce(wired):
    calls, state = wired
    state["topic_subs"] = []
    state["search_posts"]["default"] = [
        dict(_post("/r/memes/only", "niche meme"), score=5),
    ]
    got = await gather_candidates(["obscure hobby"])
    # Too few candidates to afford pruning — keep the niche meme.
    assert [p["permalink"] for p in got] == ["/r/memes/only"]


@pytest.mark.asyncio
async def test_judge_sees_vote_counts(wired):
    calls, state = wired
    state["topic_subs"] = []
    state["search_posts"]["default"] = [
        dict(_post("/r/memes/high", "banger"), score=90000),
    ]
    ai = _FakeAI("1")
    await find_relevant_meme(ai, ["cats"], topic_label="cats")
    assert "(90k↑)" in ai.prompts[-1]


def test_pick_prompt_prefers_upvoted_loosely_relevant():
    from ipedro.prompts import MEME_PICK_PROMPT
    assert "BEATS" in MEME_PICK_PROMPT
    assert "upvot" in MEME_PICK_PROMPT.lower()
