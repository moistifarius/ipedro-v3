"""The bounded tool loop in AIClient.chat_with_tools, and its wiring into
the main reply path.

The fake Anthropic client snapshots ``messages`` per call: the loop grows
one conversation list across rounds, so a captured reference would show
every round sending the final state.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import introspection
from ipedro.handlers import chat
from ipedro.memory.context_builder import BuiltContext
from ipedro.openai_client import _MAX_TOOL_ROUNDS, OpenAIClient
from tests.test_addressed import _handler, _mention_rt
from tests.test_captcha_intercept import _msg

TOOLS = introspection.tools_for(owner_dm=False)
OWNER = 315660812


def _tool_use(*calls):
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="tool_use", id=f"tu_{i}", name=name, input=args)
            for i, (name, args) in enumerate(calls)
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def _text(text):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def _client(*responses):
    """A Claude-routed client whose create() replays ``responses``."""
    sent: list[dict] = []
    queue = list(responses)

    class _Msgs:
        async def create(self, **kw):
            sent.append({**kw, "messages": list(kw["messages"])})
            return queue.pop(0)

    client = OpenAIClient(api_key=None, anthropic_api_key="k",
                          claude_model="claude-sonnet-5")
    client._anthropic = type("A", (), {"messages": _Msgs()})()
    return client, sent


def _runner(result=("Your records: nothing.", False)):
    return AsyncMock(return_value=result)


MESSAGES = [
    {"role": "system", "content": "persona", "_cache_breakpoint": True},
    {"role": "user", "content": "dale why did you ignore me"},
]


# ── the loop ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_reply_without_a_tool_call_is_one_request():
    client, sent = _client(_text("sh-sha. didn't."))
    run = _runner()
    out = await client.chat_with_tools(MESSAGES, tools=TOOLS, run_tool=run)
    assert out == "sh-sha. didn't."
    assert len(sent) == 1
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_tool_call_round_trips_and_the_answer_comes_back():
    client, sent = _client(
        _tool_use(("check_my_records", {"limit": 5})),
        _text("You said 'lol'. That's not a sentence, Matt."),
    )
    run = _runner(("2026-10-01 14:02 no_reply — policy=mention text='lol'", False))
    out = await client.chat_with_tools(MESSAGES, tools=TOOLS, run_tool=run)

    assert out == "You said 'lol'. That's not a sentence, Matt."
    run.assert_awaited_once_with("check_my_records", {"limit": 5})
    second = sent[1]["messages"]
    assert second[-2]["role"] == "assistant"            # the tool_use turn, as sent
    result = second[-1]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "tu_0"
    assert "no_reply" in result["content"]
    assert "is_error" not in result


@pytest.mark.asyncio
async def test_every_round_sends_the_same_tools_and_never_sets_tool_choice():
    """Tools sit ahead of the system prompt in the cache prefix — any change
    rebuilds every tier — and a tool_choice change costs the messages
    cache. Neither may vary within (or across) replies."""
    client, sent = _client(
        _tool_use(("check_my_records", {})), _text("done"),
    )
    await client.chat_with_tools(MESSAGES, tools=TOOLS, run_tool=_runner())
    assert json.dumps(sent[0]["tools"]) == json.dumps(sent[1]["tools"])
    assert all("tool_choice" not in s for s in sent)


@pytest.mark.asyncio
async def test_tool_rounds_keep_the_caching_and_thinking_rules():
    # Big enough to clear Sonnet 5's 1024-token cache minimum, like a real
    # persona + capability brief — below it, no breakpoint is placed at all.
    long_prefix = [
        {"role": "system", "content": "word " * 2000, "_cache_breakpoint": True},
        {"role": "user", "content": "dale why did you ignore me"},
    ]
    client, sent = _client(_tool_use(("check_my_records", {})), _text("done"))
    await client.chat_with_tools(long_prefix, tools=TOOLS, run_tool=_runner())
    for s in sent:
        assert s["thinking"] == {"type": "disabled"}
        assert "cache_control" not in s          # see _claude_kwargs
        assert s["system"][0]["cache_control"]["type"] == "ephemeral"
        assert "temperature" not in s


@pytest.mark.asyncio
async def test_parallel_calls_answer_in_one_user_message():
    """Splitting results across messages trains the model to stop making
    parallel calls."""
    client, sent = _client(
        _tool_use(("check_my_records", {}), ("check_my_records", {"limit": 3})),
        _text("done"),
    )
    run = _runner()
    await client.chat_with_tools(MESSAGES, tools=TOOLS, run_tool=run)
    assert run.await_count == 2
    last = sent[1]["messages"][-1]
    assert last["role"] == "user"
    assert [b["tool_use_id"] for b in last["content"]] == ["tu_0", "tu_1"]


@pytest.mark.asyncio
async def test_a_failed_lookup_goes_back_flagged_as_an_error():
    client, sent = _client(_tool_use(("check_my_records", {})), _text("hm"))
    await client.chat_with_tools(
        MESSAGES, tools=TOOLS, run_tool=_runner(("Couldn't get at it.", True)),
    )
    assert sent[1]["messages"][-1]["content"][0]["is_error"] is True


@pytest.mark.asyncio
async def test_a_model_that_keeps_calling_tools_is_cut_off():
    client, sent = _client(
        *[_tool_use(("check_my_records", {}))] * (_MAX_TOOL_ROUNDS + 1),
    )
    run = _runner()
    out = await client.chat_with_tools(MESSAGES, tools=TOOLS, run_tool=run)
    assert out is None
    assert len(sent) == _MAX_TOOL_ROUNDS + 1
    assert run.await_count == _MAX_TOOL_ROUNDS   # the last call is never run


@pytest.mark.asyncio
async def test_every_round_is_billed():
    client, _ = _client(_tool_use(("check_my_records", {})), _text("done"))
    db = SimpleNamespace(execute=AsyncMock())
    client.attach_usage_db(db)
    await client.chat_with_tools(
        MESSAGES, tools=TOOLS, run_tool=_runner(), chat_id=42,
    )
    assert db.execute.await_count == 2


@pytest.mark.parametrize("leaked", [
    '<invoke name="check_my_records"></invoke>',
    "check_my_records(limit=5)",
    '{"name": "check_my_records", "input": {}}',
    "<thinking>hmm</thinking> sh-sha",
])
@pytest.mark.asyncio
async def test_a_tool_call_written_out_as_text_is_never_sent(leaked):
    """Thinking-disabled failure mode: the call lands in the visible reply
    instead of being made. In a group chat that's the persona reciting
    its internals."""
    client, _ = _client(_text(leaked))
    out = await client.chat_with_tools(MESSAGES, tools=TOOLS, run_tool=_runner())
    assert out is None


@pytest.mark.parametrize("prose", [
    "Checked my records (check_my_records): I let that one pass, nobody was talking to me.",
    "check_my_health says two warnings about the hub, nothing else.",
    "I ran list_my_chats and I'm in four of them.",
    "Matt asked me to check_my_records and I did.",
])
@pytest.mark.asyncio
async def test_naming_a_tool_in_prose_is_not_a_leak(prose):
    """The brief hands the model these names. A reply that echoes one while
    explaining itself must be sent, not swallowed: silence on 'why did you
    ignore me?' is the one failure this feature can't have."""
    client, _ = _client(_text(prose))
    out = await client.chat_with_tools(
        MESSAGES, tools=TOOLS + [
            {"name": "list_my_chats"}, {"name": "check_my_health"},
        ], run_tool=_runner(),
    )
    assert out == prose


@pytest.mark.asyncio
async def test_an_ordinary_reply_passes_the_leak_guard():
    client, _ = _client(_text("I keep records, Matt. Detailed ones."))
    out = await client.chat_with_tools(MESSAGES, tools=TOOLS, run_tool=_runner())
    assert out == "I keep records, Matt. Detailed ones."


@pytest.mark.asyncio
async def test_without_tool_support_it_is_plain_chat():
    """On the OpenAI provider the loop isn't wired: same reply as before
    tools existed, never an error."""
    client = OpenAIClient(api_key="x", anthropic_api_key=None)
    assert client.supports_tools is False
    client.chat = AsyncMock(return_value="plain")
    run = _runner()
    out = await client.chat_with_tools(MESSAGES, tools=TOOLS, run_tool=run)
    assert out == "plain"
    run.assert_not_awaited()


# ── wiring into the main reply ───────────────────────────────────────────────

def _tool_rt(monkeypatch):
    rt = _mention_rt(monkeypatch)
    rt.openai = SimpleNamespace(
        supports_tools=True,
        chat=AsyncMock(return_value="plain"),
        chat_with_tools=AsyncMock(return_value="sh-sha"),
        cheap_completion=AsyncMock(return_value="NO"),
    )
    rt.settings.owner_id = OWNER
    captured: dict = {}

    async def fake_build(**kwargs):
        captured.update(kwargs)
        return BuiltContext(messages=[{"role": "user", "content": "x"}], tokens=1)

    monkeypatch.setattr(chat, "build_context", fake_build)
    return rt, captured


@pytest.mark.asyncio
async def test_a_group_reply_offers_only_that_groups_records(monkeypatch):
    rt, captured = _tool_rt(monkeypatch)
    msg = _msg(text="dale why", chat_id=-100500, user_id=OWNER)  # owner, in a GROUP
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)

    rt.openai.chat.assert_not_awaited()
    kw = rt.openai.chat_with_tools.await_args.kwargs
    assert [t["name"] for t in kw["tools"]] == ["check_my_records"]
    assert "check_my_records" in captured["capabilities"]
    assert "list_my_chats" not in captured["capabilities"]


@pytest.mark.asyncio
async def test_the_owner_dm_reply_offers_the_cross_chat_tools(monkeypatch):
    rt, captured = _tool_rt(monkeypatch)
    msg = _msg(text="why did you ignore matt in soup", chat_id=OWNER, user_id=OWNER)
    msg.chat.type = "private"
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)

    kw = rt.openai.chat_with_tools.await_args.kwargs
    assert [t["name"] for t in kw["tools"]] == [
        "check_my_records", "list_my_chats", "check_my_health",
    ]
    assert "list_my_chats" in captured["capabilities"]


@pytest.mark.asyncio
async def test_the_tool_runner_is_bound_to_this_chat(monkeypatch):
    rt, _ = _tool_rt(monkeypatch)
    rt.activity.recent = AsyncMock(return_value=[])
    msg = _msg(text="dale why", chat_id=-100500)
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)

    run_tool = rt.openai.chat_with_tools.await_args.kwargs["run_tool"]
    await run_tool("check_my_records", {})
    assert rt.activity.recent.await_args.kwargs["chat_id"] == -100500


@pytest.mark.asyncio
async def test_an_impersonation_turn_gets_no_tools(monkeypatch):
    """That turn is somebody else's voice — 'his records' don't apply."""
    rt, captured = _tool_rt(monkeypatch)
    monkeypatch.setattr(chat, "resolve_impersonation", AsyncMock(
        return_value=(SimpleNamespace(name="Luke"), ["yo", "bro"]),
    ))
    msg = _msg(text="dale act like luke")
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)

    rt.openai.chat_with_tools.assert_not_awaited()
    rt.openai.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_reaction_lands_in_the_records(monkeypatch):
    monkeypatch.setattr(chat, "_REACT_PROBABILITY", 1.0)
    rt, _ = _tool_rt(monkeypatch)
    msg = _msg(text="anyway the kitchen tap is dripping")
    await _handler(rt)(msg)
    types = [c.args[1] for c in rt.activity.log.await_args_list]
    assert "reaction" in types


# ── the same guard on the plain path (no tools offered) ──────────────────────

@pytest.mark.asyncio
async def test_a_thinking_block_in_a_plain_reply_is_never_sent():
    """Impersonation turns, hub replies and the cheap classifiers don't offer
    tools, so the loop's guard never saw them: a stray <thinking> block went
    out verbatim."""
    client, _ = _client(_text("<thinking>they want a roast</thinking> sh-sha."))
    assert await client.chat(MESSAGES, max_tokens=50) is None


@pytest.mark.asyncio
async def test_an_ordinary_plain_reply_still_goes_out():
    client, _ = _client(_text("thinking about it, sh-sha."))   # the word, no markup
    assert await client.chat(MESSAGES, max_tokens=50) == "thinking about it, sh-sha."


# ── a reply the token cap cut off never posts stopping mid-word ──────────────

def _cut(text):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="max_tokens",
        usage=SimpleNamespace(input_tokens=10, output_tokens=300),
    )


@pytest.mark.asyncio
async def test_a_reply_cut_off_at_the_cap_is_trimmed_to_its_last_sentence():
    client, _ = _client(_cut("First point made. Second point made in full. And the third poi"))
    out = await client.chat(MESSAGES, max_tokens=50)
    assert out == "First point made. Second point made in full."


@pytest.mark.asyncio
async def test_with_no_sentence_worth_keeping_it_is_left_whole_with_an_ellipsis():
    client, _ = _client(_cut("Ok. and then it just kept going and going without ever stopping for brea"))
    out = await client.chat(MESSAGES, max_tokens=50)
    assert out.endswith("brea…") and out.startswith("Ok. and then")


@pytest.mark.asyncio
async def test_a_reply_that_finished_is_untouched():
    client, _ = _client(_text("First point made. And the third poi"))      # end_turn
    assert await client.chat(MESSAGES, max_tokens=50) == "First point made. And the third poi"


@pytest.mark.asyncio
async def test_the_tool_loop_trims_a_cut_off_reply_too():
    client, _ = _client(_cut("Done and dusted. Then I started to expl"))
    out = await client.chat_with_tools(MESSAGES, tools=TOOLS, run_tool=_runner())
    assert out == "Done and dusted."
