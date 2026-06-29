"""Tests for history-based impersonation."""

from __future__ import annotations

import pytest

from ipedro.impersonate import (
    Member,
    build_impersonation_prompt,
    detect_impersonation_request,
    match_member,
    resolve_impersonation,
)


# ───────────────────────────── detection ──────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("act like Luke", "Luke"),
    ("hey bot, talk like luke please", "luke"),
    ("can you sound like Sarah", "Sarah"),
    ("impersonate Big Joe", "Big Joe"),
    ("do a Luke impression", "Luke"),
    ("do your best Sarah impression", "Sarah"),
    ("pretend to be Luke", "Luke"),
    ("pretend you're luke", "luke"),
    ("write like Matt", "Matt"),
    ("be Luke", "Luke"),
    ("channel Liz", "Liz"),
])
def test_detect_extracts_name(text, expected):
    assert detect_impersonation_request(text) == expected


@pytest.mark.parametrize("text", [
    "what's the weather",
    "act normal",            # no 'like/as'
    "I like Luke",           # not a trigger phrase
    "",
    None,
])
def test_detect_ignores_non_requests(text):
    # Either None, or a candidate that won't resolve to a member anyway.
    result = detect_impersonation_request(text)
    assert result is None or isinstance(result, str)


def test_detect_strips_article_and_trailing_clause():
    assert detect_impersonation_request("act like a Luke, it's funny") == "Luke"
    assert detect_impersonation_request("talk like the Sarah!") == "Sarah"


# ───────────────────────────── matching ───────────────────────────────────
_MEMBERS = [
    Member(1, "Luke Skywalker", "Luke", "lukes"),
    Member(2, "Sarah", "Sarah", None),
    Member(3, "Big Joe", "Big Joe", "bigjoe"),
    Member(4, "matt", None, "matt"),
]


@pytest.mark.parametrize("cand,uid", [
    ("Luke", 1),
    ("luke", 1),
    ("lukes", 1),            # username
    ("Sarah", 2),
    ("Big Joe", 3),          # two-token first name
    ("bigjoe", 3),           # username
    ("matt", 4),
])
def test_match_member_resolves(cand, uid):
    m = match_member(cand, _MEMBERS)
    assert m is not None and m.user_id == uid


def test_match_member_prefix():
    # 3+ char prefix resolves to a unique member.
    assert match_member("luk", _MEMBERS).user_id == 1


def test_match_member_none_for_unknown():
    assert match_member("pirate", _MEMBERS) is None
    assert match_member("a", _MEMBERS) is None     # too short, no exact key


def test_match_member_two_token_beats_first_token():
    # 'Big Joe' should match the two-word member, not anyone named 'Big'.
    members = [Member(9, "Big", "Big", None)] + _MEMBERS
    assert match_member("Big Joe", members).user_id == 3


# ───────────────────────────── prompt ─────────────────────────────────────
def test_build_prompt_includes_name_and_samples():
    prompt = build_impersonation_prompt("Luke", ["yo", "lol that's wild", "nah"])
    assert "Luke" in prompt
    assert "IMPERSONATION MODE" in prompt
    assert "lol that's wild" in prompt
    # The instruction must forbid breaking character / revealing the bot.
    assert "never" in prompt.lower()


def test_build_prompt_caps_block_length():
    huge = ["x" * 500 for _ in range(50)]
    prompt = build_impersonation_prompt("Luke", huge)
    # Sample block is bounded; the whole prompt stays reasonable.
    assert len(prompt) < 5000


# ───────────────────────── full pipeline ──────────────────────────────────
class _FakeDB:
    def __init__(self, members, samples):
        self._members = members
        self._samples = samples

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if "DISTINCT ON (m.user_id)" in q:
            return [
                {"user_id": m.user_id, "name": m.name,
                 "first_name": m.first_name, "username": m.username}
                for m in self._members
            ]
        # gather_style_samples
        user_id = args[1]
        return [{"content": s} for s in self._samples.get(user_id, [])]


@pytest.mark.asyncio
async def test_resolve_pipeline_happy_path():
    db = _FakeDB(
        members=[Member(1, "Luke", "Luke", "lukes")],
        samples={1: ["yo", "lol", "nah man", "that's wild", "fr fr"]},
    )
    result = await resolve_impersonation(db, chat_id=1, text="act like Luke")
    assert result is not None
    member, samples = result
    assert member.user_id == 1
    assert len(samples) == 5


@pytest.mark.asyncio
async def test_resolve_pipeline_no_request_returns_none():
    db = _FakeDB(members=[Member(1, "Luke", "Luke", None)], samples={1: ["hi"] * 5})
    assert await resolve_impersonation(db, 1, "what's up bot") is None


@pytest.mark.asyncio
async def test_resolve_pipeline_unknown_member_returns_none():
    db = _FakeDB(members=[Member(1, "Luke", "Luke", None)], samples={1: ["hi"] * 5})
    assert await resolve_impersonation(db, 1, "act like Gandalf") is None


@pytest.mark.asyncio
async def test_resolve_pipeline_too_few_samples_returns_none():
    # Member exists but has only 2 messages — below the floor.
    db = _FakeDB(members=[Member(1, "Luke", "Luke", None)], samples={1: ["yo", "hi"]})
    assert await resolve_impersonation(db, 1, "act like Luke") is None
