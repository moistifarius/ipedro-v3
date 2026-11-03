"""The advisor: a stronger model Dale's reply-writing one may consult mid-reply.

What is pinned here is what goes wrong quietly if it breaks: the request shape
(the beta endpoint, the tool at the END of the list so a chat's cache prefix
holds), which text is the reply (never the narration before the call), what is
billed to whom, and that a pairing the API refuses costs one request and not
every reply.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest

from ipedro import bot as bot_module
from ipedro import capabilities
from ipedro.capabilities import capability_brief
from ipedro.config import Settings
from ipedro.handlers import admin, chat
from ipedro.openai_client import (
    ADVISOR_BETA, ADVISOR_TOOL_TYPE, OpenAIClient, _claude_text,
    _claude_text_price,
)
from tests.test_identity import _as_hank
from tests.test_captcha_intercept import _msg
from tests.test_tool_loop import _handler, _tool_rt

MESSAGES = [
    {"role": "system", "content": "persona", "_cache_breakpoint": True},
    {"role": "user", "content": "rusty is the moon landing real"},
]
RECORDS_TOOL = {
    "name": "check_my_records", "description": "d",
    "input_schema": {"type": "object", "properties": {}},
}


# ── fake responses ───────────────────────────────────────────────────────────

def _text(t):
    return SimpleNamespace(type="text", text=t)


def _asked():
    return SimpleNamespace(type="server_tool_use", id="srvtoolu_1", name="advisor", input={})


def _advice(kind="advisor_redacted_result"):
    return SimpleNamespace(
        type="advisor_tool_result", tool_use_id="srvtoolu_1",
        content=SimpleNamespace(type=kind))


def _resp(*blocks, stop="end_turn", iterations=None, out=5):
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=out, cache_creation_input_tokens=0,
        cache_read_input_tokens=0, iterations=iterations)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage)


def _it(kind, model=None, i=100, o=50, cr=0):
    return SimpleNamespace(type=kind, model=model, input_tokens=i, output_tokens=o,
                           cache_creation_input_tokens=0, cache_read_input_tokens=cr)


class _Endpoint:
    """Records every request; replays queued responses or raises."""

    def __init__(self, *queue):
        self.sent: list[dict] = []
        self.queue = list(queue)

    async def create(self, **kw):
        self.sent.append({**kw, "messages": list(kw["messages"])})
        nxt = self.queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _client(plain=(), beta=(), *, advisor="claude-opus-5-5", executor="claude-sonnet-5", **kw):
    plain, beta = _Endpoint(*plain), _Endpoint(*beta)
    c = OpenAIClient(api_key=None, anthropic_api_key="k", claude_model=executor,
                     advisor_model=advisor, **kw)
    c._anthropic = SimpleNamespace(
        messages=plain, beta=SimpleNamespace(messages=beta))
    return c, plain, beta


def _bad_request(msg="advisor: invalid pair"):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(
        msg, response=httpx.Response(400, request=req), body=None)


# ── the request ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_advisor_request_goes_to_the_beta_endpoint_with_the_tool_and_header():
    c, plain, beta = _client(beta=[_resp(_text("hi"))])
    assert await c.chat(MESSAGES, max_tokens=50, advisor=True) == "hi"
    assert plain.sent == []
    (req,) = beta.sent
    assert req["betas"] == [ADVISOR_BETA] == ["advisor-tool-2026-03-01"]
    assert req["tools"] == [{
        "type": "advisor_20260301", "name": "advisor",
        "model": "claude-opus-5-5", "max_tokens": 1024, "max_uses": 1,
    }]


@pytest.mark.asyncio
async def test_a_request_that_does_not_ask_for_it_is_exactly_what_it_was():
    c, plain, beta = _client(plain=[_resp(_text("hi"))])
    assert await c.chat(MESSAGES, max_tokens=50) == "hi"
    assert beta.sent == []
    assert "betas" not in plain.sent[0] and "tools" not in plain.sent[0]


@pytest.mark.asyncio
async def test_with_no_advisor_model_set_asking_for_one_does_nothing():
    c, plain, beta = _client(plain=[_resp(_text("hi"))], advisor=None)
    await c.chat(MESSAGES, max_tokens=50, advisor=True)
    assert beta.sent == [] and "tools" not in plain.sent[0]


@pytest.mark.asyncio
async def test_the_advisors_output_is_capped_and_never_below_the_floor():
    c, _, beta = _client(beta=[_resp(_text("a")), _resp(_text("b"))],
                         advisor_max_tokens=100)
    await c.chat(MESSAGES, advisor=True, max_tokens=50)
    assert beta.sent[0]["tools"][0]["max_tokens"] == 1024
    c.advisor_max_tokens = 2048
    await c.chat(MESSAGES, advisor=True, max_tokens=50)
    assert beta.sent[1]["tools"][0]["max_tokens"] == 2048


@pytest.mark.asyncio
async def test_max_uses_is_the_configured_cap_and_zero_means_uncapped():
    c, _, beta = _client(beta=[_resp(_text("a")), _resp(_text("b"))], advisor_max_uses=3)
    await c.chat(MESSAGES, advisor=True, max_tokens=50)
    assert beta.sent[0]["tools"][0]["max_uses"] == 3
    c.advisor_max_uses = 0
    await c.chat(MESSAGES, advisor=True, max_tokens=50)
    assert "max_uses" not in beta.sent[1]["tools"][0]


@pytest.mark.asyncio
async def test_the_advisor_goes_last_and_a_chats_tool_list_never_varies():
    """Tools head the cache prefix; the list for one chat must be
    byte-identical from reply to reply."""
    c, _, beta = _client(beta=[_resp(_text("a")), _resp(_text("b"))])
    await c.chat_with_tools(MESSAGES, tools=[RECORDS_TOOL], run_tool=AsyncMock(),
                            max_tokens=50, advisor=True)
    await c.chat_with_tools(MESSAGES, tools=[RECORDS_TOOL], run_tool=AsyncMock(),
                            max_tokens=50, advisor=True)
    first, second = (json.dumps(r["tools"]) for r in beta.sent)
    assert first == second
    assert [t["name"] for t in beta.sent[0]["tools"]] == ["check_my_records", "advisor"]


@pytest.mark.asyncio
async def test_the_other_tools_are_untouched_when_the_advisor_is_added():
    c, _, beta = _client(beta=[_resp(_text("a"))])
    await c.chat_with_tools(MESSAGES, tools=[RECORDS_TOOL], run_tool=AsyncMock(),
                            max_tokens=50, advisor=True)
    assert beta.sent[0]["tools"][0] == RECORDS_TOOL
    assert RECORDS_TOOL == {"name": "check_my_records", "description": "d",
                            "input_schema": {"type": "object", "properties": {}}}


@pytest.mark.asyncio
async def test_thinking_and_sampling_rules_are_the_same_with_the_advisor():
    for executor in ("claude-sonnet-5", "claude-sonnet-5-5", "claude-opus-5-5"):
        c, plain, beta = _client(plain=[_resp(_text("a"))], beta=[_resp(_text("a"))],
                                 executor=executor)
        await c.chat(MESSAGES, max_tokens=50)
        await c.chat(MESSAGES, max_tokens=50, advisor=True)
        a, b = plain.sent[0], beta.sent[0]
        for key in ("model", "max_tokens", "thinking", "output_config", "temperature", "system"):
            assert a.get(key) == b.get(key), (executor, key)


# ── what the reply is ────────────────────────────────────────────────────────

def test_only_what_comes_after_the_advice_is_the_reply():
    r = _resp(_text("hold on, let me check with my guy"), _asked(), _advice(),
              _text("yeah the moon landing happened. sh-sha."))
    assert _claude_text(r) == "yeah the moon landing happened. sh-sha."


def test_with_two_consults_only_the_text_after_the_last_counts():
    r = _resp(_text("one sec"), _asked(), _advice(), _text("hm, wait"),
              _asked(), _advice(), _text("final answer"))
    assert _claude_text(r) == "final answer"


def test_a_failed_consult_still_ends_the_narration():
    r = _resp(_text("checking"), _asked(), _advice("advisor_tool_result_error"), _text("fine, from me:"))
    assert _claude_text(r) == "fine, from me:"


def test_text_after_the_advice_that_is_empty_is_no_reply_not_the_narration():
    r = _resp(_text("let me ask"), _asked(), _advice())
    assert _claude_text(r) is None


def test_a_reply_with_no_consult_is_every_text_block_as_before():
    r = _resp(_text("one"), _text("two"))
    assert _claude_text(r) == "one\ntwo"


@pytest.mark.asyncio
async def test_a_consult_end_to_end_posts_only_the_answer():
    c, _, beta = _client(beta=[_resp(
        _text("let me think on that"), _asked(), _advice(), _text("yes it's real"))])
    assert await c.chat(MESSAGES, max_tokens=50, advisor=True) == "yes it's real"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "<advisor>the moon landing</advisor>", "</advisor_tool_result>",
    "<server_tool_use id='x'>", "<thinking>they want a roast</thinking> sh-sha",
])
async def test_advisor_markup_in_the_reply_is_withheld(text):
    c, _, _ = _client(beta=[_resp(_text(text))])
    assert await c.chat(MESSAGES, max_tokens=50, advisor=True) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "my tax advisor (Gary) says it's real",
    "advisor {sic} said so", "ask your financial advisor (not me)",
])
async def test_the_word_advisor_in_a_sentence_is_a_sentence(text):
    c, _, _ = _client(beta=[_resp(_text(text))])
    assert await c.chat(MESSAGES, max_tokens=50, advisor=True) == text


# ── a paused turn ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_turn_paused_on_the_advisor_is_sent_back_as_it_came():
    paused = _resp(_text("one sec"), _asked(), stop="pause_turn")
    done = _resp(_advice(), _text("here you go"))
    c, _, beta = _client(beta=[paused, done])
    assert await c.chat(MESSAGES, max_tokens=50, advisor=True) == "here you go"
    resend = beta.sent[1]["messages"]
    assert resend[-1] == {"role": "assistant", "content": paused.content}
    assert resend[:-1] == beta.sent[0]["messages"]
    assert resend[-1]["content"][-1].type == "server_tool_use"      # the pending call, kept
    assert beta.sent[1]["tools"] == beta.sent[0]["tools"]            # advisor still present


@pytest.mark.asyncio
async def test_pauses_are_followed_a_few_times_not_forever():
    pause = lambda: _resp(_asked(), stop="pause_turn")        # noqa: E731
    c, _, beta = _client(beta=[pause() for _ in range(10)])
    assert await c.chat(MESSAGES, max_tokens=50, advisor=True) is None
    assert len(beta.sent) == 1 + 3


@pytest.mark.asyncio
async def test_the_tool_loop_follows_a_pause_then_answers():
    paused = _resp(_asked(), stop="pause_turn")
    done = _resp(_advice(), _text("worked it out"))
    c, _, beta = _client(beta=[paused, done])
    out = await c.chat_with_tools(MESSAGES, tools=[RECORDS_TOOL], run_tool=AsyncMock(),
                                  max_tokens=50, advisor=True)
    assert out == "worked it out" and len(beta.sent) == 2


@pytest.mark.asyncio
async def test_the_tool_loop_still_runs_my_tools_alongside_the_advisor():
    call = SimpleNamespace(type="tool_use", id="tu_1", name="check_my_records", input={})
    first = _resp(_text("checking"), call, stop="tool_use")
    final = _resp(_text("nothing in there"))
    c, _, beta = _client(beta=[first, final])
    run = AsyncMock(return_value=("no rows", False))
    out = await c.chat_with_tools(MESSAGES, tools=[RECORDS_TOOL], run_tool=run,
                                  max_tokens=50, advisor=True)
    assert out == "nothing in there"
    run.assert_awaited_once_with("check_my_records", {})
    assert [t["name"] for t in beta.sent[1]["tools"]] == ["check_my_records", "advisor"]


# ── a pairing the API refuses ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_refused_pairing_costs_one_retry_then_replies_go_without_it():
    c, plain, beta = _client(plain=[_resp(_text("a")), _resp(_text("b"))],
                             beta=[_bad_request()])
    assert await c.chat(MESSAGES, max_tokens=50, advisor=True) == "a"
    assert len(beta.sent) == 1 and len(plain.sent) == 1
    assert "tools" not in plain.sent[0] and "betas" not in plain.sent[0]
    assert c.advisor_turned_down and not c.advisor_available()
    # and the next reply doesn't even try
    assert await c.chat(MESSAGES, max_tokens=50, advisor=True) == "b"
    assert len(beta.sent) == 1 and len(plain.sent) == 2


@pytest.mark.asyncio
async def test_the_other_tools_survive_the_retry_without_the_advisor():
    c, plain, _ = _client(plain=[_resp(_text("a"))], beta=[_bad_request()])
    await c.chat_with_tools(MESSAGES, tools=[RECORDS_TOOL], run_tool=AsyncMock(),
                            max_tokens=50, advisor=True)
    assert plain.sent[0]["tools"] == [RECORDS_TOOL]


@pytest.mark.asyncio
async def test_a_request_that_fails_without_the_advisor_too_is_not_blamed_on_it():
    c, plain, _ = _client(plain=[_bad_request("model: nope")], beta=[_bad_request("model: nope")])
    with pytest.raises(anthropic.BadRequestError):
        await c._claude_create(c._claude_kwargs(
            model="claude-sonnet-5", system=None,
            chat_messages=[{"role": "user", "content": "x"}], max_tokens=5,
            temperature=1.0, tools=[c._advisor_tool("claude-sonnet-5")]))
    assert not c.advisor_turned_down


@pytest.mark.asyncio
async def test_only_a_bad_request_triggers_the_fallback():
    req = httpx.Request("POST", "https://x")
    boom = anthropic.InternalServerError("down", response=httpx.Response(500, request=req), body=None)
    c, plain, _ = _client(beta=[boom])
    with pytest.raises(anthropic.InternalServerError):
        await c._claude_create(c._claude_kwargs(
            model="claude-sonnet-5", system=None,
            chat_messages=[{"role": "user", "content": "x"}], max_tokens=5,
            temperature=1.0, tools=[c._advisor_tool("claude-sonnet-5")]))
    assert plain.sent == [] and not c.advisor_turned_down


@pytest.mark.asyncio
async def test_changing_the_advisor_or_the_models_forgets_the_refusal():
    c, _, _ = _client(plain=[_resp(_text("a"))], beta=[_bad_request()])
    await c.chat(MESSAGES, max_tokens=50, advisor=True)
    assert c.advisor_turned_down
    c.set_advisor_model("claude-opus-4-8")
    assert c.advisor_available() and not c.advisor_turned_down
    c.set_advisor_model("claude-opus-5-5")
    c.set_claude_model("claude-opus-5-5")               # a different pairing altogether
    assert c.advisor_available()


# ── availability ─────────────────────────────────────────────────────────────

def test_the_advisor_is_available_only_where_it_can_work():
    c, _, _ = _client()
    assert c.advisor_available()
    c.set_advisor_model(None)
    assert not c.advisor_available()
    c.set_advisor_model("   ")
    assert c.advisor_model is None and not c.advisor_available()
    c.set_advisor_model("claude-opus-5-5")
    c._text_provider = "openai"
    assert not c.advisor_available()                    # tools are Claude-path only
    no_key = OpenAIClient(api_key="x", advisor_model="claude-opus-5-5")
    assert not no_key.advisor_available()


# ── who pays for what ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_advisors_tokens_are_billed_at_the_advisors_rates_and_apart():
    its = [_it("message", i=400, o=89), _it("advisor_message", "claude-opus-5-5", i=800, o=600, cr=200),
           _it("message", i=1300, o=440)]
    c, _, _ = _client(beta=[_resp(_text("a"), iterations=its, out=529)])
    logged = []
    c._log_usage = AsyncMock(side_effect=lambda **kw: logged.append(kw))
    await c.chat(MESSAGES, max_tokens=50, chat_id=42, advisor=True)
    kinds = [(k["kind"], k["model"]) for k in logged]
    assert kinds == [("chat", "claude-sonnet-5"), ("advisor", "claude-opus-5-5")]
    adv = logged[1]
    assert adv["chat_id"] == 42
    assert adv["prompt_tokens"] == 800 + 200 and adv["completion_tokens"] == 600
    assert adv["cache_read_tokens"] == 200
    assert adv["cost_usd"] == pytest.approx(_claude_text_price("claude-opus-5-5", 800, 600, 0, 200))
    assert logged[0]["completion_tokens"] == 529              # the executor's own, untouched


@pytest.mark.asyncio
async def test_no_iterations_means_no_advisor_row():
    c, _, _ = _client(beta=[_resp(_text("a"), iterations=None)])
    c._log_usage = AsyncMock()
    await c.chat(MESSAGES, max_tokens=50, advisor=True)
    assert [k.kwargs["kind"] for k in c._log_usage.await_args_list] == ["chat"]


@pytest.mark.asyncio
async def test_an_advisor_iteration_with_no_model_is_priced_as_the_configured_one():
    c, _, _ = _client(beta=[_resp(_text("a"), iterations=[_it("advisor_message", None, 10, 10)])])
    c._log_usage = AsyncMock()
    await c.chat(MESSAGES, max_tokens=50, advisor=True)
    assert c._log_usage.await_args_list[1].kwargs["model"] == "claude-opus-5-5"


@pytest.mark.asyncio
async def test_each_paused_request_is_billed_once():
    its = [_it("advisor_message", "claude-opus-5-5", 100, 100)]
    c, _, _ = _client(beta=[_resp(_asked(), stop="pause_turn"),
                            _resp(_advice(), _text("ok"), iterations=its)])
    c._log_usage = AsyncMock()
    await c.chat(MESSAGES, max_tokens=50, advisor=True)
    assert [k.kwargs["kind"] for k in c._log_usage.await_args_list] == ["chat", "chat", "advisor"]


# ── /ai_advisor's probe ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_probe_asks_the_beta_endpoint_with_the_candidate_and_the_real_executor():
    c, _, beta = _client(beta=[_resp(_text("pong"))], executor="claude-sonnet-5")
    assert await c.probe_advisor_model("claude-opus-4-8") is None
    (req,) = beta.sent
    assert req["betas"] == [ADVISOR_BETA] and req["model"] == "claude-sonnet-5"
    assert req["max_tokens"] == 8
    assert req["tools"][0]["model"] == "claude-opus-4-8"
    assert req["tools"][0]["type"] == ADVISOR_TOOL_TYPE


@pytest.mark.asyncio
async def test_the_probe_says_why_a_pairing_is_refused():
    c, _, _ = _client(beta=[_bad_request("the advisor must be at least as capable")])
    why = await c.probe_advisor_model("claude-haiku-5-5")
    assert why and "at least as capable" in why


@pytest.mark.asyncio
async def test_the_probe_without_a_key_has_an_answer():
    c = OpenAIClient(api_key="x")
    assert "No Anthropic key" in await c.probe_advisor_model("claude-opus-5-5")


# ── the brief ────────────────────────────────────────────────────────────────

def test_the_brief_only_mentions_the_advisor_where_he_has_one():
    off = capability_brief()
    assert "advisor" not in off.lower()
    assert capability_brief(advisor=False) == off
    on = capability_brief(advisor=True)
    assert capabilities._ADVISOR in on and on != off
    assert on.replace("\n\n" + capabilities._ADVISOR, "") == off          # nothing else moved


def test_the_brief_asks_him_to_use_it_rarely_and_asks_the_advisor_to_be_brief():
    text = capabilities._ADVISOR
    for phrase in ("Ask rarely", "Never for banter", "not twice",
                   "Don't mention it", "(Advisor:", "under 100 words"):
        assert phrase in text, phrase


# ── in the chat ──────────────────────────────────────────────────────────────

def _advising_rt(monkeypatch, *, available=True):
    rt, captured = _tool_rt(monkeypatch)
    rt.openai.advisor_available = lambda: available
    return rt, captured


async def _say(rt, text="rusty is the moon landing real", **kw):
    msg = _msg(text=text, **kw)
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=9))
    await _handler(rt)(msg)
    return msg


@pytest.mark.asyncio
async def test_dale_is_given_the_advisor_and_told_how_to_use_it(monkeypatch):
    rt, captured = _advising_rt(monkeypatch)
    await _say(rt)
    assert rt.openai.chat_with_tools.await_args.kwargs["advisor"] is True
    assert capabilities._ADVISOR in captured["capabilities"]


@pytest.mark.asyncio
async def test_without_one_available_nothing_changes(monkeypatch):
    rt, captured = _advising_rt(monkeypatch, available=False)
    await _say(rt)
    assert "advisor" not in rt.openai.chat_with_tools.await_args.kwargs
    assert "advisor" not in captured["capabilities"].lower()


@pytest.mark.asyncio
async def test_a_client_that_knows_nothing_of_advisors_is_fine(monkeypatch):
    rt, captured = _tool_rt(monkeypatch)                    # no advisor_available attr
    await _say(rt)
    assert "advisor" not in rt.openai.chat_with_tools.await_args.kwargs


@pytest.mark.asyncio
async def test_no_other_bot_gets_one(monkeypatch):
    rt, captured = _advising_rt(monkeypatch)
    _as_hank(rt)
    await _say(rt, text="hank is the moon landing real")
    assert "advisor" not in rt.openai.chat_with_tools.await_args.kwargs
    assert "advisor" not in captured["capabilities"].lower()


@pytest.mark.asyncio
async def test_an_impersonation_turn_gets_none(monkeypatch):
    rt, _ = _advising_rt(monkeypatch)
    monkeypatch.setattr(chat, "resolve_impersonation", AsyncMock(
        return_value=(SimpleNamespace(name="Luke"), ["yo", "bro"])))
    await _say(rt, text="rusty act like luke")
    assert "advisor" not in rt.openai.chat.await_args.kwargs


@pytest.mark.asyncio
async def test_the_plain_path_gets_it_too(monkeypatch):
    rt, _ = _advising_rt(monkeypatch)
    rt.openai.supports_tools = False                          # no tools offered
    await _say(rt)
    assert rt.openai.chat.await_args.kwargs["advisor"] is True


# ── /ai_advisor ──────────────────────────────────────────────────────────────

def _cmd(text):
    return SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private", title=None),
        from_user=SimpleNamespace(id=7, is_bot=False, username="admin"),
        text=text, reply=AsyncMock())


def _admin_rt(*, probe=None, advisor="claude-opus-5-5", blocked=False, dale=True):
    ai = SimpleNamespace(
        claude_model="claude-sonnet-5", advisor_model=advisor,
        advisor_max_tokens=1024, advisor_max_uses=1, advisor_turned_down=blocked,
        probe_advisor_model=AsyncMock(return_value=probe),
    )
    ai.set_advisor_model = lambda m: setattr(ai, "advisor_model", m)
    settings = SimpleNamespace(admin_ids=frozenset({7}), manages_bots=dale)
    if not dale:
        settings.bot_name, settings.bot_flavor = "Hank", "plain"
    return SimpleNamespace(settings=settings, openai=ai, db=SimpleNamespace())


def _ai_advisor(rt):
    return next(h.callback for h in admin.build_router(rt).observers["message"].handlers
                if h.callback.__name__ == "ai_advisor")


@pytest.fixture
def kv(monkeypatch):
    saved = {}

    async def kv_set(db, key, value):
        saved[key] = value

    monkeypatch.setattr(admin, "kv_set", kv_set)
    return saved


@pytest.mark.asyncio
async def test_show_says_who_advises_whom_and_what_it_costs(kv):
    msg = _cmd("/ai_advisor")
    await _ai_advisor(_admin_rt())(msg)
    out = msg.reply.await_args.args[0]
    assert "claude-opus-5-5" in out and "claude-sonnet-5" in out
    assert "1 consult a reply" in out and "1024 tokens" in out
    assert "/cost" in out and kv == {}


@pytest.mark.asyncio
async def test_show_says_so_when_off_and_when_the_api_turned_it_down(kv):
    msg = _cmd("/ai_advisor show")
    await _ai_advisor(_admin_rt(advisor=None))(msg)
    assert "Advisor: off" in msg.reply.await_args.args[0]
    msg2 = _cmd("/ai_advisor show")
    await _ai_advisor(_admin_rt(blocked=True))(msg2)
    assert "turned the pairing" in msg2.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_another_bot_is_told_it_never_consults_one(kv):
    msg = _cmd("/ai_advisor show")
    await _ai_advisor(_admin_rt(dale=False))(msg)
    assert "Only Dale" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_off_turns_it_off_and_it_stays_off(kv):
    rt = _admin_rt()
    msg = _cmd("/ai_advisor off")
    await _ai_advisor(rt)(msg)
    assert rt.openai.advisor_model is None and kv == {"advisor_model": "off"}
    assert bot_module.resolve_advisor_model(kv["advisor_model"], "claude-opus-5-5") is None


@pytest.mark.asyncio
async def test_a_model_the_api_accepts_is_set_and_saved(kv):
    rt = _admin_rt(probe=None)
    msg = _cmd("/ai_advisor claude-opus-4-8")
    await _ai_advisor(rt)(msg)
    rt.openai.probe_advisor_model.assert_awaited_once_with("claude-opus-4-8")
    assert rt.openai.advisor_model == "claude-opus-4-8"
    assert kv == {"advisor_model": "claude-opus-4-8"}
    assert "now claude-opus-4-8" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_a_model_the_api_refuses_changes_nothing(kv):
    rt = _admin_rt(probe="advisor must be at least as capable")
    msg = _cmd("/ai_advisor claude-haiku-5-5")
    await _ai_advisor(rt)(msg)
    assert rt.openai.advisor_model == "claude-opus-5-5" and kv == {}
    out = msg.reply.await_args.args[0]
    assert "won't take claude-haiku-5-5" in out and "at least as capable" in out


@pytest.mark.asyncio
async def test_only_an_admin_may(kv):
    rt = _admin_rt()
    msg = _cmd("/ai_advisor off")
    msg.from_user.id = 99
    await _ai_advisor(rt)(msg)
    assert rt.openai.advisor_model == "claude-opus-5-5" and kv == {}


# ── startup and settings ─────────────────────────────────────────────────────

@pytest.mark.parametrize("saved, configured, expected", [
    (None, "claude-opus-5-5", "claude-opus-5-5"),      # nothing saved: the setting
    ("off", "claude-opus-5-5", None),                   # turned off with /ai_advisor
    ("claude-opus-4-8", "claude-opus-5-5", "claude-opus-4-8"),   # the saved one wins
    (None, "", None), (None, None, None), ("", "  ", None),
])
def test_what_the_bot_starts_with(saved, configured, expected):
    assert bot_module.resolve_advisor_model(saved, configured) == expected


def test_the_settings_defaults():
    s = Settings(telegram_bot_token="t", openai_api_key="k", database_url="postgresql://t/t")
    assert s.advisor_model == "claude-opus-5-5"
    assert (s.advisor_max_tokens, s.advisor_max_uses) == (1024, 1)
