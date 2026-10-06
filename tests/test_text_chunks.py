"""Fitting text into Telegram's 4096-character messages."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers.admin import build_router
from ipedro.text_chunks import DEFAULT_LIMIT, TELEGRAM_LIMIT, chunk_lines


def test_short_input_is_one_chunk():
    assert chunk_lines(["a", "b", "c"]) == ["a\nb\nc"]


def test_nothing_in_nothing_out():
    assert chunk_lines([]) == []
    assert chunk_lines(["", ""]) == []


def test_lines_are_not_split_when_they_fit():
    lines = [f"line {i:03d} " + "x" * 90 for i in range(100)]
    chunks = chunk_lines(lines, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n".join(chunks).split("\n") == lines          # nothing lost or reordered
    assert len(chunks) > 1


def test_a_line_longer_than_the_limit_is_cut_not_sent_oversize():
    """The /logs chunker put a whole traceback in one chunk whenever a
    single log entry was longer than the cap."""
    huge = ("word " * 2000).strip()
    chunks = chunk_lines(["first", huge, "last"], limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert " ".join(c.replace("\n", " ") for c in chunks).split() == (
        ["first"] + huge.split() + ["last"]
    )


def test_a_line_with_no_spaces_is_still_cut():
    chunks = chunk_lines(["x" * 2500], limit=1000)
    assert [len(c) for c in chunks] == [1000, 1000, 500]


def test_no_chunk_is_ever_empty_and_blank_separators_survive_inside():
    chunks = chunk_lines(["a", "", "b"], limit=100)
    assert chunks == ["a\n\nb"]
    assert all(chunks)


def test_the_default_limit_leaves_room_for_a_header():
    assert DEFAULT_LIMIT + 250 < TELEGRAM_LIMIT


def test_random_input_always_fits_and_loses_nothing():
    rng = random.Random(7)
    for _ in range(200):
        lines = [
            " ".join("w" * rng.randint(1, 30) for _ in range(rng.randint(0, 80)))
            for _ in range(rng.randint(0, 40))
        ]
        limit = rng.choice([50, 200, 1000])
        chunks = chunk_lines(lines, limit=limit)
        assert all(0 < len(c) <= limit for c in chunks)
        # every character survives, in order (a word is only ever cut when
        # it straddles the limit with no space in the back half to break at)
        squash = lambda parts: "".join(parts).replace(" ", "").replace("\n", "")
        assert squash(lines) == squash(chunks)
        if limit >= 200:                      # real-world sizes: words stay whole
            assert " ".join(lines).split() == " ".join(chunks).split()


# ── the admin commands that print lists ──────────────────────────────────────

def _handler(rt, name):
    router = build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == name)


def _msg(text):
    return SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private", title=None),
        from_user=SimpleNamespace(id=7, is_bot=False, username="admin"),
        text=text, reply=AsyncMock(),
    )


def _sent(msg):
    return [c.args[0] for c in msg.reply.await_args_list]


@pytest.mark.asyncio
async def test_activity_200_is_split_not_rejected_by_telegram():
    """/activity clamps N to 200 and sent every row in one message: any N
    above ~35 produced a reply Telegram refuses."""
    rows = [
        dict(chat_id=-100123456789, message_id=i, event_type="no_reply",
             detail="policy=mention text=" + repr("x" * 60),
             created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        for i in range(200)
    ]
    rt = SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset({7})),
        activity=SimpleNamespace(recent=AsyncMock(return_value=rows)),
    )
    msg = _msg("/activity 200")
    await _handler(rt, "activity_cmd")(msg)
    sent = _sent(msg)
    assert len(sent) > 1
    assert all(len(s) <= TELEGRAM_LIMIT for s in sent)
    assert sum(s.count("no_reply") for s in sent) == 200      # every row got out


@pytest.mark.asyncio
async def test_cmdlog_with_a_long_stored_error_still_fits():
    rows = [dict(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), chat_id=1,
                 user_id=2, command="/x", success=False, error="boom " * 2000)] * 3
    rt = SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset({7})),
        command_log=SimpleNamespace(tail=AsyncMock(return_value=rows)),
    )
    msg = _msg("/cmdlog")
    await _handler(rt, "cmdlog")(msg)
    assert all(len(s) <= TELEGRAM_LIMIT for s in _sent(msg))


@pytest.mark.asyncio
async def test_memory_facts_all_no_empty_first_message_when_one_chat_is_huge():
    """One chat whose facts alone exceeded the cap used to leave an empty
    first message, then an oversized one."""
    facts = [SimpleNamespace(id=i, fact="fact " + "y" * 270) for i in range(100)]
    rt = SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset({7})),
        chats=SimpleNamespace(list_known=AsyncMock(return_value=[
            {"chat_id": -1001, "title": "Soup", "type": "supergroup"},
        ])),
        memory=SimpleNamespace(list_facts=AsyncMock(return_value=facts)),
    )
    msg = _msg("/memory_facts_all")
    await _handler(rt, "memory_facts_all")(msg)
    sent = _sent(msg)
    assert all(s.strip() for s in sent)
    assert all(len(s) <= TELEGRAM_LIMIT for s in sent)
    assert sum(s.count("fact ") for s in sent) >= 100
    assert "Total: 100 facts across 1 chat(s)." in sent[-1]


@pytest.mark.asyncio
async def test_logs_one_enormous_entry_is_cut():
    rt = SimpleNamespace(settings=SimpleNamespace(admin_ids=frozenset({7})))
    import ipedro.handlers.admin as admin
    original = admin.recent_log_lines
    admin.recent_log_lines = lambda limit, contains: ["Traceback " + "frame " * 3000]
    try:
        msg = _msg("/logs")
        await _handler(rt, "logs_cmd")(msg)
    finally:
        admin.recent_log_lines = original
    sent = _sent(msg)
    assert len(sent) > 1 and all(len(s) <= TELEGRAM_LIMIT for s in sent)
