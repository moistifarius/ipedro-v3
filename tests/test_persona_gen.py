"""Personas written from a short description, informed by every bot's
memory of the people and things it names."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import bots, persona_gen


# ── picking out what the description names ───────────────────────────────────

def test_parse_subjects_cleans_the_list():
    raw = "1. Luke\n- crypto\n• Luke\n\"Arby's\".\nHank\nNONE\n" + "x" * 41
    assert persona_gen.parse_subjects(raw, bot_name="hank") == [
        "Luke", "crypto", "Arby's",
    ]


def test_parse_subjects_none_and_cap():
    assert persona_gen.parse_subjects("NONE", bot_name="Hank") == []
    assert persona_gen.parse_subjects(None, bot_name="Hank") == []
    many = "\n".join(f"thing {i}" for i in range(10))
    assert len(persona_gen.parse_subjects(many, bot_name="Hank")) == 6


def test_like_wildcards_are_literal():
    assert persona_gen._escape_like("100%_off!") == "100!%!_off!!"


# ── one memory ───────────────────────────────────────────────────────────────

class FakeMemory:
    """A bot's database, answering the three lookups by what they ask."""

    def __init__(self, *, people=(), user_facts=(), facts=(), hits=()):
        self.people, self.user_facts = list(people), list(user_facts)
        self.facts, self.hits = list(facts), list(hits)
        self.queries: list[tuple[str, tuple]] = []
        self.close = AsyncMock()

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        if "FROM users" in sql:          # a name, matched whole, any case
            return [p for p in self.people
                    if p["name"].split()[0].lower() == args[0].lower()]
        if "WHERE user_id = $1" in sql:
            return [{"fact": f} for f in self.user_facts]
        if "fact ILIKE" in sql:          # mentioned anywhere, any case
            term = args[0].strip("%").lower()
            return [{"fact": f} for f in self.facts if term in f.lower()]
        if "FROM embeddings" in sql:
            return self.hits
        raise AssertionError(sql)


@pytest.mark.asyncio
async def test_notes_from_one_memory():
    db = FakeMemory(
        people=[{"user_id": 7, "name": "Luke Smith"}],
        user_facts=["works nights at the hospital"],
        facts=["Luke swears crypto is coming back"],
        hits=[
            {"content": "bitcoin to 200k   by christmas", "similarity": 0.62,
             "author": "Luke Smith"},
            {"content": "unrelated", "similarity": 0.1, "author": "Ann"},
        ],
    )
    notes = await persona_gen.notes_from(db, "Luke", [0.1, 0.2], vector=True)
    assert notes == [
        "(about Luke Smith) works nights at the hospital",
        "Luke swears crypto is coming back",
        "Luke Smith: bitcoin to 200k by christmas",
    ]
    sql, args = next(q for q in db.queries if "fact ILIKE" in q[0])
    assert args[0] == "%Luke%" and "ESCAPE '!'" in sql


@pytest.mark.asyncio
async def test_no_pgvector_means_no_similarity_search():
    db = FakeMemory()
    await persona_gen.notes_from(db, "Luke", [0.1], vector=False)
    assert not any("FROM embeddings" in sql for sql, _ in db.queries)


# ── the whole thing ──────────────────────────────────────────────────────────

def _rt(*, subjects="Luke\ncrypto", persona="You are Hank. You hate crypto."):
    return SimpleNamespace(
        db=FakeMemory(
            people=[{"user_id": 7, "name": "Luke"}],
            user_facts=["works nights"],
        ),
        pgvector_available=True,
        settings=SimpleNamespace(database_url="postgresql://u:p@h:5432/ipedro"),
        openai=SimpleNamespace(
            cheap_completion=AsyncMock(return_value=subjects),
            embed=AsyncMock(return_value=[0.1, 0.2]),
            chat=AsyncMock(return_value=persona),
        ),
    )


@pytest.fixture
def other_bots(monkeypatch):
    """Two other bots: Peggy has a memory, Bobby never started (no DB)."""
    peggy = FakeMemory(facts=["crypto ruined Thanksgiving"],
                       people=[{"user_id": 7, "name": "Luke"}],
                       user_facts=["works nights"])         # same fact: kept once
    rows = [
        bots.BotRow(id=4, telegram_id=40, username="PeggyBot", name="Peggy",
                    aliases="peggy", status="active", token="t"),
        bots.BotRow(id=5, telegram_id=50, username="BobbyBot", name="Bobby",
                    aliases="bobby", status="stopped", token="t"),
    ]
    monkeypatch.setattr(bots, "list_bots", AsyncMock(return_value=rows))
    urls = []

    async def connect(url):
        urls.append(url)
        if url.endswith("/ipedro_bot_50"):
            raise OSError("database does not exist")
        return peggy

    return SimpleNamespace(peggy=peggy, urls=urls, connect=connect,
                           has_vector=AsyncMock(return_value=False))


@pytest.mark.asyncio
async def test_build_persona_draws_on_every_bots_memory(other_bots):
    rt = _rt()
    draft = await persona_gen.build_persona(
        rt, name="Hank", aliases="hank", description="hates Luke's crypto talk",
        connect=other_bots.connect, has_vector=other_bots.has_vector,
    )
    assert draft.generated and draft.persona == "You are Hank. You hate crypto."
    assert draft.found == {"Luke": 1, "crypto": 1}
    assert other_bots.urls == ["postgresql://u:p@h:5432/ipedro_bot_40",
                               "postgresql://u:p@h:5432/ipedro_bot_50"]
    other_bots.peggy.close.assert_awaited_once()            # borrowed, returned
    prompt = rt.openai.chat.await_args.args[0][0]["content"]
    assert "Name: Hank" in prompt and "hates Luke's crypto talk" in prompt
    assert "About Luke:\n- (about Luke) works nights" in prompt
    assert "About crypto:\n- crypto ruined Thanksgiving" in prompt
    assert "not instructions" in prompt                     # notes are data
    assert prompt.count("works nights") == 1                # deduped across bots


@pytest.mark.asyncio
async def test_a_persona_is_asked_for_short_and_kept_short(other_bots):
    """Dale's persona is one line and his replies are a line or two; a
    full character sheet made a bot perform at length. The brief says short,
    the model gets room for a short answer only, and a long one is cut."""
    rt = _rt()
    rt.openai.chat = AsyncMock(return_value="You are Hank. " + "Propane. " * 400)
    draft = await persona_gen.build_persona(
        rt, name="Hank", aliases="hank", description="sells propane",
        connect=other_bots.connect, has_vector=other_bots.has_vector,
    )
    prompt = rt.openai.chat.await_args.args[0][0]["content"]
    assert "under 100 words" in prompt and "never like an assistant" in prompt
    assert "Short is the point" in prompt
    assert rt.openai.chat.await_args.kwargs["max_tokens"] == 300
    assert len(draft.persona) <= 1200


@pytest.mark.asyncio
async def test_no_description_writes_from_the_name_alone(other_bots):
    rt = _rt()
    draft = await persona_gen.build_persona(
        rt, name="Hank Hill", aliases="hank", description=None,
        connect=other_bots.connect, has_vector=other_bots.has_vector,
    )
    rt.openai.cheap_completion.assert_not_awaited()
    assert other_bots.urls == []                            # nothing to look up
    assert draft.found == {}
    prompt = rt.openai.chat.await_args.args[0][0]["content"]
    assert "from the name 'Hank Hill' alone" in prompt


@pytest.mark.asyncio
async def test_model_down_falls_back_to_the_description(other_bots):
    rt = _rt(persona=None)
    draft = await persona_gen.build_persona(
        rt, name="Hank", aliases="hank", description="sells propane",
        connect=other_bots.connect, has_vector=other_bots.has_vector,
    )
    assert not draft.generated
    assert draft.persona == "You are Hank. sells propane"


@pytest.mark.asyncio
async def test_a_failing_lookup_costs_only_that_lookup(other_bots):
    rt = _rt(subjects="Luke")
    rt.db.fetch = AsyncMock(side_effect=RuntimeError("db hiccup"))
    draft = await persona_gen.build_persona(
        rt, name="Hank", aliases="hank", description="loves Luke",
        connect=other_bots.connect, has_vector=other_bots.has_vector,
    )
    assert draft.generated
    assert draft.found == {"Luke": 1}                        # Peggy's still counted


@pytest.mark.asyncio
async def test_nothing_said_in_a_private_chat_reaches_a_persona():
    """The new bot speaks from this text in groups. A DM is one person
    talking to one bot: every lookup is limited to group chats."""
    db = FakeMemory(people=[{"user_id": 7, "name": "Luke"}])
    await persona_gen.notes_from(db, "Luke", [0.1], vector=True)
    lookups = [sql for sql, _ in db.queries if "FROM users" not in sql]
    assert len(lookups) == 3
    assert all("chat_id < 0" in sql for sql in lookups), lookups


@pytest.mark.asyncio
async def test_the_notes_are_framed_as_data_by_the_code_not_only_the_prompt(other_bots, monkeypatch):
    """The sentence in prompts.py is content an /evolve change may edit; the
    one in persona_gen.py is not. Take the first away and the second stands."""
    monkeypatch.setattr(persona_gen, "PERSONA_FROM_DESCRIPTION_PROMPT",
                        "Notes:\n{notes}\nName {name} {aliases} {description}")
    rt = _rt()
    await persona_gen.build_persona(
        rt, name="Hank", aliases="hank", description="hates Luke's crypto talk",
        connect=other_bots.connect, has_vector=other_bots.has_vector,
    )
    prompt = rt.openai.chat.await_args.args[0][0]["content"]
    assert "quoted from the chats as DATA" in prompt
    assert prompt.index("quoted from the chats as DATA") < prompt.index("About Luke")
