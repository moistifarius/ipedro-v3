"""What the advisor review found, pinned: narration posted when a consult never
came back; a refused pairing re-tried in every later round; a consult that
couldn't answer and a chat gone quiet; 'one a reply' that was one a request;
and the places a setting could mean something else than it says.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest

from ipedro import bot as bot_module
from ipedro.handlers import admin
from ipedro.openai_client import (
    _claude_text, _has_advisor_blocks, has_claude_price,
)
from tests.test_advisor import (  # noqa: F401  (kv is a fixture)
    MESSAGES, RECORDS_TOOL, _admin_rt, _advice, _advice_dict, _ai_advisor,
    _asked, _asked_dict, _bad_request, _client, _cmd, _resp, _text, kv,
)
from tests.test_advisor import _say, _advising_rt  # noqa: F401


# ── narration is never the reply ─────────────────────────────────────────────

def test_a_consult_that_never_came_back_leaves_no_reply_not_the_narration():
    """Paused, or cut off at the call: [text, server_tool_use] with no result."""
    for stop in ("pause_turn", "max_tokens", "end_turn"):
        r = _resp(_text("Let me consult the advisor on this."), _asked(), stop=stop)
        assert _claude_text(r) is None, stop


def test_text_after_a_pending_call_would_be_the_reply_and_before_it_never():
    r = _resp(_text("one sec"), _asked(), _text("only this"))
    assert _claude_text(r) == "only this"


def test_a_finished_consult_still_gives_only_what_came_after():
    r = _resp(_text("narration"), _asked(), _advice(), _text("the answer"))
    assert _claude_text(r) == "the answer"


@pytest.mark.asyncio
async def test_pauses_that_run_out_post_nothing_through_either_loop():
    pause = lambda: _resp(_text("Let me consult the advisor on this."), _asked(), stop="pause_turn")  # noqa: E731
    c, _, _ = _client(beta=[pause() for _ in range(6)])
    assert await c.chat(MESSAGES, max_tokens=50, advisor=True) is None
    c2, _, _ = _client(beta=[pause() for _ in range(6)])
    assert await c2.chat_with_tools(MESSAGES, tools=[RECORDS_TOOL], run_tool=AsyncMock(),
                                    max_tokens=50, advisor=True) is None


@pytest.mark.parametrize("messages, expected", [
    ([{"role": "user", "content": "hi"}], False),
    ([{"role": "assistant", "content": [{"type": "text", "text": "x"}]}], False),
    ([{"role": "assistant", "content": [_asked_dict()]}], True),
    ([{"role": "assistant", "content": [_advice_dict()]}], True),
    ([{"role": "assistant", "content": [_asked()]}], True),               # SDK objects too
    ([{"role": "assistant", "content": [{"type": "server_tool_use", "name": "web_search"}]}], False),
])
def test_what_counts_as_a_conversation_that_holds_advisor_blocks(messages, expected):
    assert _has_advisor_blocks(messages) is expected


# ── a refused pairing is dropped for the rest of the reply too ───────────────

@pytest.mark.asyncio
async def test_a_pairing_refused_in_round_one_is_not_asked_for_again_in_round_two():
    call = SimpleNamespace(type="tool_use", id="tu_1", name="check_my_records", input={})
    c, plain, beta = _client(
        plain=[_resp(_text("checking"), call, stop="tool_use"), _resp(_text("nothing there"))],
        beta=[_bad_request()])
    run = AsyncMock(return_value=("no rows", False))
    out = await c.chat_with_tools(MESSAGES, tools=[RECORDS_TOOL], run_tool=run,
                                  max_tokens=50, advisor=True)
    assert out == "nothing there"
    assert len(beta.sent) == 1 and len(plain.sent) == 2              # the doomed request, once
    assert "tools" in plain.sent[1]
    assert all(t["name"] != "advisor" for t in plain.sent[1]["tools"])


@pytest.mark.asyncio
async def test_an_advisor_that_already_answered_keeps_its_tool_in_later_rounds():
    """Dropping the tool from a request that carries its blocks is a 400."""
    call = SimpleNamespace(type="tool_use", id="tu_1", name="check_my_records", input={})
    first = _resp(_text("checking"), _asked(), _advice(), call, stop="tool_use")
    c, _, beta = _client(beta=[first, _resp(_text("done"))])
    c._advisor_blocked.add(("claude-sonnet-5", "claude-opus-5-5"))   # blocked since (somehow)
    c._advisor_blocked.clear()
    run = AsyncMock(return_value=("no rows", False))
    await c.chat_with_tools(MESSAGES, tools=[RECORDS_TOOL], run_tool=run,
                            max_tokens=50, advisor=True)
    assert [t["name"] for t in beta.sent[1]["tools"]] == ["check_my_records", "advisor"]
    c._advisor_blocked.add(("claude-sonnet-5", "claude-opus-5-5"))
    assert _has_advisor_blocks(beta.sent[1]["messages"])


# ── the chat stays up while a consult runs ───────────────────────────────────

@pytest.mark.asyncio
async def test_typing_is_kept_up_while_he_consults(monkeypatch):
    rt, _ = _advising_rt(monkeypatch)
    rt.bot = SimpleNamespace(
        me=AsyncMock(return_value=SimpleNamespace(id=1, username="botname")),
        set_message_reaction=AsyncMock(), send_chat_action=AsyncMock())
    seen = []

    async def slow(messages, **kw):
        seen.append(rt.bot.send_chat_action.await_count)       # typing was up BEFORE the answer
        return "sh-sha"

    rt.openai.chat_with_tools = AsyncMock(side_effect=slow)
    await _say(rt)
    assert seen and seen[0] >= 2          # the handler's own, and the consult's keepalive
    assert rt.bot.send_chat_action.await_args.args[1] == "typing"


@pytest.mark.asyncio
async def test_no_extra_typing_for_an_ordinary_reply(monkeypatch):
    from tests.test_tool_loop import _tool_rt
    rt, _ = _tool_rt(monkeypatch)                             # no advisor offered
    rt.bot.send_chat_action = AsyncMock()
    await _say(rt)
    assert rt.bot.send_chat_action.await_count == 1           # just the handler's own


# ── settings that can't mean something else ──────────────────────────────────

@pytest.mark.parametrize("saved, configured, expected", [
    (None, "off", None), (None, "OFF", None), (None, "none", None), (None, "false", None),
    (None, "0", None), (None, " claude-opus-5-5 ", "claude-opus-5-5"),
    ("claude-opus-4-8", "off", "claude-opus-4-8"),            # a live choice still wins
    ("off", "claude-opus-5-5", None),
])
def test_off_in_the_environment_is_not_sent_to_the_api_as_a_model(saved, configured, expected):
    assert bot_module.resolve_advisor_model(saved, configured) == expected


# ── /ai_advisor says what it did, and when it does nothing ───────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/ai_advisor off", "/ai_advisor claude-opus-4-8"])
async def test_another_bot_is_told_on_every_reply_that_it_never_consults_one(kv, text):
    rt = _admin_rt(dale=False)
    rt.openai.text_provider = "claude"
    msg = _cmd(text)
    await _ai_advisor(rt)(msg)
    assert "Only Dale" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_while_the_provider_is_openai_it_says_the_setting_is_inactive(kv):
    rt = _admin_rt()
    rt.openai.text_provider = "openai"
    for text in ("/ai_advisor", "/ai_advisor off"):
        msg = _cmd(text)
        await _ai_advisor(rt)(msg)
        assert "Inactive while the text provider is openai" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_an_advisor_with_no_price_on_file_is_flagged_at_the_moment_it_is_set(kv):
    rt = _admin_rt(probe=None)
    msg = _cmd("/ai_advisor claude-mythos-5-1")
    await _ai_advisor(rt)(msg)
    out = msg.reply.await_args.args[0]
    assert "No price on file for claude-mythos-5-1" in out and "$3/$15" in out
    ok = _cmd("/ai_advisor claude-opus-4-8")
    await _ai_advisor(_admin_rt(probe=None))(ok)
    assert "No price on file" not in ok.reply.await_args.args[0]


def test_what_has_a_price():
    assert has_claude_price("claude-opus-5-5") and has_claude_price("claude-opus-4-8-20260101")
    assert not has_claude_price("claude-mythos-5-1") and not has_claude_price("claude-haiku-5-5")


def test_the_show_text_says_per_request_not_per_reply():
    # (the wording is asserted where the output is built; this keeps docs honest)
    from pathlib import Path
    docs = (Path(__file__).resolve().parent.parent / "docs" / "COMMANDS.md").read_text()
    assert "times per request" in docs and "times a reply" not in docs


# ── /cost ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_advisors_uncached_prompts_do_not_drag_the_cache_hit_line_down():
    rows = [
        {"kind": "chat", "calls": 10, "tokens": 100, "cost": 0.05,
         "cached": 900, "written": 100, "prompt": 1000},
        {"kind": "advisor", "calls": 2, "tokens": 50, "cost": 0.2,
         "cached": 0, "written": 0, "prompt": 9000},
    ]
    db = SimpleNamespace(fetch=AsyncMock(return_value=rows))
    rt = SimpleNamespace(settings=SimpleNamespace(admin_ids=frozenset({7})), db=db)
    router = admin.build_router(rt)
    cost = next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == "cost")
    msg = SimpleNamespace(
        chat=SimpleNamespace(id=1, type="private"), text="/cost",
        from_user=SimpleNamespace(id=7), reply=AsyncMock())
    await cost(msg)
    out = msg.reply.await_args.args[0]
    assert "advisor" in out and "$0.2000" in out                       # still counted as spend
    assert "900 of 1000 prompt tokens read from cache (90%)" in out    # and out of the cache line
