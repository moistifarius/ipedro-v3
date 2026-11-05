"""The advisor over the real SDK, with only the network replaced.

tests/test_advisor.py drives the client with stand-in response objects. This
one lets the real anthropic SDK build the request and parse the response, from
the JSON shapes Anthropic documents for the advisor tool, so a misreading of
the API (the wrong endpoint, a block type the SDK reads differently, a paused
turn that doesn't serialize back) fails here and not in a group chat.
"""

from __future__ import annotations

import json

import httpx
import pytest
from anthropic import AsyncAnthropic

from ipedro.openai_client import ADVISOR_BETA, OpenAIClient, _claude_text

MESSAGES = [
    {"role": "system", "content": "persona", "_cache_breakpoint": True},
    {"role": "user", "content": "rusty is the moon landing real"},
]

USAGE = {"input_tokens": 412, "output_tokens": 531, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0}


def _message(content, stop="end_turn", iterations=None):
    usage = dict(USAGE)
    if iterations is not None:
        usage["iterations"] = iterations
    return {"id": "msg_1", "type": "message", "role": "assistant",
            "model": "claude-sonnet-5", "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": usage}


# The shapes in Anthropic's docs ("Successful advisor call", the encrypted
# variant, "Usage and billing").
ASKED = {"type": "server_tool_use", "id": "srvtoolu_abc123", "name": "advisor", "input": {}}
REDACTED = {"type": "advisor_tool_result", "tool_use_id": "srvtoolu_abc123",
            "content": {"type": "advisor_redacted_result",
                        "encrypted_content": "EqQBCkYIBRgCIiQ5ZjE0N2M2OC0y"}}
PLAIN = {"type": "advisor_tool_result", "tool_use_id": "srvtoolu_abc123",
         "content": {"type": "advisor_result", "text": "Say yes; the evidence is overwhelming."}}
ERRORED = {"type": "advisor_tool_result", "tool_use_id": "srvtoolu_abc123",
           "content": {"type": "advisor_tool_result_error", "error_code": "overloaded"}}
ITERATIONS = [
    {"type": "message", "input_tokens": 412, "cache_read_input_tokens": 0,
     "cache_creation_input_tokens": 0, "output_tokens": 89},
    {"type": "advisor_message", "model": "claude-opus-5-5", "input_tokens": 823,
     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 1612},
    {"type": "message", "input_tokens": 1348, "cache_read_input_tokens": 412,
     "cache_creation_input_tokens": 0, "output_tokens": 442},
]


def _wired(*responses):
    """A client whose real SDK talks to a canned transport."""
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=queue.pop(0))

    client = OpenAIClient(api_key=None, anthropic_api_key="k",
                          claude_model="claude-sonnet-5", advisor_model="claude-opus-5-5")
    client._anthropic = AsyncAnthropic(
        api_key="k", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return client, seen


def _body(req: httpx.Request) -> dict:
    return json.loads(req.content)


@pytest.mark.asyncio
async def test_the_request_the_sdk_sends_is_the_one_the_docs_describe():
    client, seen = _wired(_message([{"type": "text", "text": "yes it's real"}]))
    assert await client.chat(MESSAGES, max_tokens=50, advisor=True) == "yes it's real"
    (req,) = seen
    assert req.url.path == "/v1/messages" and req.url.params.get("beta") == "true"
    assert req.headers["anthropic-beta"] == ADVISOR_BETA
    body = _body(req)
    assert body["tools"] == [{
        "type": "advisor_20260301", "name": "advisor", "model": "claude-opus-5-5",
        "max_tokens": 1024, "max_uses": 1}]
    assert body["model"] == "claude-sonnet-5" and body["thinking"] == {"type": "disabled"}
    assert "persona" in json.dumps(body["system"])            # the system prompt rides along


@pytest.mark.asyncio
async def test_a_plain_request_is_still_the_plain_endpoint_with_no_beta_header():
    client, seen = _wired(_message([{"type": "text", "text": "hi"}]))
    await client.chat(MESSAGES, max_tokens=50)
    (req,) = seen
    assert req.url.params.get("beta") is None
    assert "anthropic-beta" not in req.headers
    assert "tools" not in _body(req)


@pytest.mark.asyncio
@pytest.mark.parametrize("advice", [REDACTED, PLAIN, ERRORED])
async def test_the_reply_is_what_follows_the_advice_whichever_variant_it_is(advice):
    client, _ = _wired(_message([
        {"type": "text", "text": "Let me consult the advisor on this."},
        ASKED, advice,
        {"type": "text", "text": "yeah the moon landing happened. sh-sha."},
    ], iterations=ITERATIONS))
    assert await client.chat(MESSAGES, max_tokens=50, advisor=True) == (
        "yeah the moon landing happened. sh-sha.")


@pytest.mark.asyncio
async def test_the_sdk_parses_the_documented_blocks_into_what_the_extractor_reads():
    client, _ = _wired(_message([
        {"type": "text", "text": "narration"}, ASKED, REDACTED,
        {"type": "text", "text": "the answer"}], iterations=ITERATIONS))
    resp = await client._claude_create(client._claude_kwargs(
        model="claude-sonnet-5", system=None,
        chat_messages=[{"role": "user", "content": "x"}], max_tokens=50,
        temperature=1.0, tools=[client._advisor_tool("claude-sonnet-5")]))
    assert [b.type for b in resp.content] == [
        "text", "server_tool_use", "advisor_tool_result", "text"]
    assert _claude_text(resp) == "the answer"


@pytest.mark.asyncio
async def test_the_advisors_iteration_is_read_off_the_real_usage_object():
    client, _ = _wired(_message([{"type": "text", "text": "ok"}], iterations=ITERATIONS))
    logged = []

    async def spy(**kw):
        logged.append(kw)

    client._log_usage = spy
    await client.chat(MESSAGES, max_tokens=50, chat_id=7, advisor=True)
    assert [(k["kind"], k["model"]) for k in logged] == [
        ("chat", "claude-sonnet-5"), ("advisor", "claude-opus-5-5")]
    assert logged[1]["prompt_tokens"] == 823 and logged[1]["completion_tokens"] == 1612


@pytest.mark.asyncio
async def test_a_paused_turn_serializes_back_exactly_as_it_came():
    paused = _message([{"type": "text", "text": "one sec"}, ASKED], stop="pause_turn")
    done = _message([REDACTED, {"type": "text", "text": "here you go"}])
    client, seen = _wired(paused, done)
    assert await client.chat(MESSAGES, max_tokens=50, advisor=True) == "here you go"
    second = _body(seen[1])
    assert second["messages"][-1]["role"] == "assistant"
    sent_back = second["messages"][-1]["content"]
    assert [b["type"] for b in sent_back] == ["text", "server_tool_use"]
    assert sent_back[1]["id"] == "srvtoolu_abc123" and sent_back[1]["name"] == "advisor"
    assert sent_back[1]["input"] == {}
    assert seen[1].headers["anthropic-beta"] == ADVISOR_BETA
    assert second["tools"] == _body(seen[0])["tools"]


@pytest.mark.asyncio
async def test_a_resolved_advisor_result_also_round_trips_through_the_tool_loop():
    """The executor called the advisor AND one of ours in one turn: the advice
    block goes back with the tool result and the SDK must accept it."""
    call = {"type": "tool_use", "id": "toolu_1", "name": "check_my_records", "input": {}}
    first = _message([{"type": "text", "text": "checking"}, ASKED, REDACTED, call],
                     stop="tool_use")
    final = _message([{"type": "text", "text": "nothing in there"}])
    client, seen = _wired(first, final)
    tool = {"name": "check_my_records", "description": "d",
            "input_schema": {"type": "object", "properties": {}}}

    async def run(name, args):
        return "no rows", False

    out = await client.chat_with_tools(MESSAGES, tools=[tool], run_tool=run,
                                       max_tokens=50, advisor=True)
    assert out == "nothing in there"
    back = _body(seen[1])["messages"]
    assert [b["type"] for b in back[-2]["content"]] == [
        "text", "server_tool_use", "advisor_tool_result", "tool_use"]
    assert back[-2]["content"][2]["content"]["type"] == "advisor_redacted_result"
    assert back[-1]["content"][0]["type"] == "tool_result"
