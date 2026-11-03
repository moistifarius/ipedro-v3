"""Pins for the advisor that survived mutation testing of tests/test_advisor.py
and tests/test_advisor_wire.py (each docstring names what stayed green)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import bot as bot_module
from ipedro.config import Settings
from ipedro.openai_client import ADVISOR_BETA, _claude_text_price
from tests.test_advisor import (  # noqa: F401  (kv is a fixture)
    MESSAGES, RECORDS_TOOL, _admin_rt, _advice, _ai_advisor, _asked,
    _bad_request, _client, _cmd, _it, _resp, _text, kv,
)


async def _tools_chat(c, **kw):
    return await c.chat_with_tools(
        MESSAGES, tools=[RECORDS_TOOL], run_tool=AsyncMock(), max_tokens=50, **kw)


# ── the request ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_request_names_the_advisor_that_is_configured_not_the_default():
    """Survived: `"model": self.advisor_model` -> `"model": "claude-opus-5-5"`.
    Every other test uses the default pair, so a hard-coded model passed."""
    c, _, beta = _client(beta=[_resp(_text("a")), _resp(_text("b"))],
                         advisor="claude-opus-4-8")
    await c.chat(MESSAGES, max_tokens=50, advisor=True)
    c.set_advisor_model("claude-mythos-5-1")
    await c.chat(MESSAGES, max_tokens=50, advisor=True)
    assert [r["tools"][0]["model"] for r in beta.sent] == [
        "claude-opus-4-8", "claude-mythos-5-1"]


@pytest.mark.asyncio
async def test_the_constructor_settings_reach_the_request():
    """Survived: `self.advisor_max_tokens = advisor_max_tokens` ->
    `= _ADVISOR_MIN_TOKENS` (ADVISOR_MAX_TOKENS ignored). The existing test sets
    the attribute AFTER construction, which is not the path bot.py takes."""
    c, _, beta = _client(beta=[_resp(_text("a"))], advisor_max_tokens=2048,
                         advisor_max_uses=3)
    await c.chat(MESSAGES, max_tokens=50, advisor=True)
    tool = beta.sent[0]["tools"][0]
    assert tool["max_tokens"] == 2048 and tool["max_uses"] == 3


@pytest.mark.asyncio
async def test_the_constructor_trims_the_advisor_model():
    """Survived: no .strip() in __init__ (only set_advisor_model's was tested)."""
    c, _, beta = _client(beta=[_resp(_text("a"))], advisor="  claude-opus-5-5 ")
    assert c.advisor_model == "claude-opus-5-5"
    await c.chat(MESSAGES, max_tokens=50, advisor=True)
    assert beta.sent[0]["tools"][0]["model"] == "claude-opus-5-5"


@pytest.mark.asyncio
async def test_the_tool_loop_adds_no_advisor_to_a_reply_that_did_not_ask():
    """Survived: `if advisor else None` dropped in _chat_claude_tools. The plain
    path had this test; the tool loop (which every Dale reply takes) did not,
    so an advisor model set + advisor=False would still bill an advisor."""
    c, plain, beta = _client(plain=[_resp(_text("a"))])
    await _tools_chat(c)
    assert beta.sent == []
    assert plain.sent[0]["tools"] == [RECORDS_TOOL]


@pytest.mark.asyncio
async def test_the_tool_loop_without_an_advisor_model_sends_no_advisor():
    c, plain, beta = _client(plain=[_resp(_text("a"))], advisor=None)
    await _tools_chat(c, advisor=True)
    assert beta.sent == [] and plain.sent[0]["tools"] == [RECORDS_TOOL]


@pytest.mark.asyncio
async def test_a_tool_loop_fallback_to_plain_chat_keeps_the_advisor():
    """Survived: `advisor=advisor` dropped from chat_with_tools' fallback to
    chat() (no tools / OpenAI-less path)."""
    c, _, beta = _client(beta=[_resp(_text("a"))])
    out = await c.chat_with_tools(MESSAGES, tools=[], run_tool=AsyncMock(),
                                  max_tokens=50, advisor=True)
    assert out == "a" and beta.sent[0]["tools"][0]["name"] == "advisor"


# ── the probe ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_probe_sends_exactly_the_tool_the_replies_will_send():
    """Survived: probe tool name -> 'adv', probe tool max_tokens -> 100 (the API
    floor is 1024, so /ai_advisor would refuse EVERY model), max_uses dropped.
    The probe test only looked at model and type."""
    c, _, beta = _client(beta=[_resp(_text("pong"))])
    assert await c.probe_advisor_model("claude-opus-4-8") is None
    (req,) = beta.sent
    assert req["tools"] == [{
        "type": "advisor_20260301", "name": "advisor",
        "model": "claude-opus-4-8", "max_tokens": 1024, "max_uses": 1}]
    live = c._advisor_tool("claude-sonnet-5")
    assert {k: v for k, v in req["tools"][0].items() if k != "model"} == {
        k: v for k, v in live.items() if k != "model"}


@pytest.mark.asyncio
async def test_the_probe_reports_a_non_api_failure_instead_of_raising():
    """Survived: removing the generic `except Exception` (a transport error that
    is not an APIError would crash the /ai_advisor handler)."""
    c, _, _ = _client(beta=[RuntimeError("boom")])
    why = await c.probe_advisor_model("claude-opus-4-8")
    assert why and "boom" in why


@pytest.mark.asyncio
async def test_the_probe_returns_the_api_message_when_refused():
    """Survived: `return str(exc)[:300]` -> `return None` (a refused model
    would be accepted and saved)."""
    c, _, _ = _client(beta=[_bad_request("nope")])
    assert "nope" in (await c.probe_advisor_model("x") or "")


# ── a refused pairing ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_setting_the_same_advisor_again_forgets_a_refusal():
    """Survived: `self._advisor_blocked.clear()` removed. The key is the PAIR, so
    a different model never needed the clear; the case it exists for is
    /ai_advisor <same id> after the beta was switched on (or the pair
    otherwise fixed), which stayed blocked until a restart."""
    c, _, _ = _client(plain=[_resp(_text("a"))], beta=[_bad_request()])
    await c.chat(MESSAGES, max_tokens=50, advisor=True)
    assert c.advisor_turned_down and not c.advisor_available()
    c.set_advisor_model("claude-opus-5-5")                      # same id
    assert c.advisor_available() and not c.advisor_turned_down


@pytest.mark.asyncio
async def test_turned_down_is_about_the_current_pairing_only():
    """Survived: advisor_turned_down -> bool(self._advisor_blocked). After
    /ai_model moves the executor, /ai_advisor show would say the API turned
    the (new, untried) pairing down while replies are in fact carrying the
    advisor."""
    c, _, _ = _client(plain=[_resp(_text("a"))], beta=[_bad_request()])
    await c.chat(MESSAGES, max_tokens=50, advisor=True)
    assert c.advisor_turned_down
    c.set_claude_model("claude-opus-5-5")
    assert c.advisor_available() and not c.advisor_turned_down


@pytest.mark.asyncio
async def test_a_refusal_is_blocked_whatever_the_400_says():
    """Survived: fallback narrowed to 400s whose message mentions 'advisor'.
    Anthropic's wording for a bad pairing / a beta that is not on is not
    something the code may depend on."""
    c, plain, _ = _client(plain=[_resp(_text("a"))],
                          beta=[_bad_request("tools.0: input should be a valid dictionary")])
    assert await c.chat(MESSAGES, max_tokens=50, advisor=True) == "a"
    assert c.advisor_turned_down


@pytest.mark.asyncio
async def test_a_refusal_in_the_tool_loop_is_retried_without_the_advisor_and_blocks():
    c, plain, beta = _client(plain=[_resp(_text("a")), _resp(_text("b"))],
                             beta=[_bad_request()])
    assert await _tools_chat(c, advisor=True) == "a"
    assert plain.sent[0]["tools"] == [RECORDS_TOOL] and "betas" not in plain.sent[0]
    assert c.advisor_turned_down
    assert await _tools_chat(c, advisor=True) == "b"            # next reply: no beta attempt
    assert len(beta.sent) == 1


# ── paused turns in the tool loop ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_tool_loop_sends_the_paused_turn_back_as_it_came():
    """Survived: `convo.append({"role": "assistant", ...})` removed from the tool
    loop's resume (the resent request was identical to the first, so the
    advisor call would be re-made from scratch every time). The chat loop's
    twin of this test existed; the tool loop's asserted only the answer."""
    paused = _resp(_text("one sec"), _asked(), stop="pause_turn")
    c, _, beta = _client(beta=[paused, _resp(_advice(), _text("done"))])
    assert await _tools_chat(c, advisor=True) == "done"
    first, second = (r["messages"] for r in beta.sent)
    assert second[:-1] == first
    assert second[-1] == {"role": "assistant", "content": paused.content}
    assert beta.sent[1]["tools"] == beta.sent[0]["tools"]
    assert beta.sent[1]["betas"] == [ADVISOR_BETA]


@pytest.mark.asyncio
async def test_the_tool_loop_follows_pauses_a_few_times_not_forever():
    """Survived: tool loop's own range(_MAX_PAUSE_RESUMES) -> range(10) / range(1)
    (the 'not forever' test drives only chat())."""
    pause = lambda: _resp(_asked(), stop="pause_turn")        # noqa: E731
    c, _, beta = _client(beta=[pause() for _ in range(12)])
    assert await _tools_chat(c, advisor=True) is None
    assert len(beta.sent) == 1 + 3


@pytest.mark.asyncio
async def test_a_pause_in_the_second_tool_round_is_followed_too():
    call = SimpleNamespace(type="tool_use", id="tu_1", name="check_my_records", input={})
    first = _resp(_text("checking"), call, stop="tool_use")
    paused = _resp(_asked(), stop="pause_turn")
    final = _resp(_advice(), _text("nothing in there"))
    c, _, beta = _client(beta=[first, paused, final])
    c_run = AsyncMock(return_value=("no rows", False))
    out = await c.chat_with_tools(MESSAGES, tools=[RECORDS_TOOL], run_tool=c_run,
                                  max_tokens=50, advisor=True)
    assert out == "nothing in there" and len(beta.sent) == 3
    assert beta.sent[2]["messages"][-1]["role"] == "assistant"
    assert all([t["name"] for t in r["tools"]] == ["check_my_records", "advisor"]
               for r in beta.sent)


# ── what is posted ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "my tax advisor (Gary) says it's real",
    "advisor {sic} said so", "ask your financial advisor (not me)",
])
async def test_the_word_advisor_in_a_sentence_survives_the_tool_loop_too(text):
    """Survived: 'advisor' added to the names offered to _written_out_call in
    the tool loop (the existing sentence test only drives chat())."""
    c, _, _ = _client(beta=[_resp(_text(text))])
    assert await _tools_chat(c, advisor=True) == text


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["<advisor>the moon landing</advisor>",
                                  "</advisor_tool_result>", "<server_tool_use id='x'>"])
async def test_advisor_markup_is_withheld_in_the_tool_loop_too(text):
    c, _, _ = _client(beta=[_resp(_text(text))])
    assert await _tools_chat(c, advisor=True) is None


# ── billing ──────────────────────────────────────────────────────────────────

def _it_cw(model, i, o, cw, cr):
    it = _it("advisor_message", model, i, o, cr)
    it.cache_creation_input_tokens = cw
    return it


@pytest.mark.asyncio
async def test_the_advisors_cache_writes_are_billed_and_logged():
    """Survived: advisor `icw` zeroed / left out of prompt_tokens, cost or
    cache_write_tokens (the test helper pinned cache_creation_input_tokens at
    0). With `caching` on, or the advisor's prefix cached, this is real money."""
    c, _, _ = _client(beta=[_resp(_text("a"), iterations=[
        _it_cw("claude-opus-5-5", 800, 600, 3000, 200)])])
    logged = []
    c._log_usage = AsyncMock(side_effect=lambda **kw: logged.append(kw))
    await c.chat(MESSAGES, max_tokens=50, chat_id=1, advisor=True)
    adv = next(k for k in logged if k["kind"] == "advisor")
    assert adv["prompt_tokens"] == 800 + 3000 + 200
    assert (adv["cache_write_tokens"], adv["cache_read_tokens"]) == (3000, 200)
    assert adv["cost_usd"] == pytest.approx(
        _claude_text_price("claude-opus-5-5", 800, 600, 3000, 200))
    assert adv["cost_usd"] > _claude_text_price("claude-opus-5-5", 800, 600, 0, 200)


@pytest.mark.asyncio
async def test_the_advisor_model_the_api_reports_is_the_one_priced():
    """Survived: `am = self.advisor_model or model` (iteration's own model
    ignored). It matters after /ai_advisor changes the model mid-request."""
    its = [_it("advisor_message", "claude-opus-4-8", 100, 100)]
    c, _, _ = _client(beta=[_resp(_text("a"), iterations=its)])     # configured: opus-5-5
    c._log_usage = AsyncMock()
    await c.chat(MESSAGES, max_tokens=50, advisor=True)
    assert c._log_usage.await_args_list[1].kwargs["model"] == "claude-opus-4-8"
    assert c._log_usage.await_args_list[1].kwargs["cost_usd"] == pytest.approx(
        _claude_text_price("claude-opus-4-8", 100, 100, 0, 0))


@pytest.mark.asyncio
async def test_the_tool_loop_bills_the_advisor_too():
    """Survived: removing the usage log from _claude_tool_round, i.e. NO advisor
    row for any reply that takes the tool loop (all of Dale's)."""
    its = [_it("advisor_message", "claude-opus-5-5", 800, 600)]
    c, _, _ = _client(beta=[_resp(_text("a"), iterations=its)])
    c._log_usage = AsyncMock()
    await _tools_chat(c, advisor=True)
    assert [k.kwargs["kind"] for k in c._log_usage.await_args_list] == ["chat", "advisor"]


# ── /ai_advisor ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_show_for_dale_has_no_other_bot_note_and_floors_the_token_cap(kv):
    """Survived: the 'Only Dale' note appended always; max(1024, ..) -> min."""
    rt = _admin_rt()
    rt.openai.advisor_max_tokens = 2048
    msg = _cmd("/ai_advisor show")
    await _ai_advisor(rt)(msg)
    out = msg.reply.await_args.args[0]
    assert "Only Dale" not in out and "2048 tokens" in out
    rt.openai.advisor_max_tokens = 100                    # below the API floor
    msg = _cmd("/ai_advisor show")
    await _ai_advisor(rt)(msg)
    assert "1024 tokens" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["none", "disable", "disabled", "OFF"])
async def test_the_words_for_off(kv, word):
    rt = _admin_rt()
    await _ai_advisor(rt)(_cmd(f"/ai_advisor {word}"))
    assert rt.openai.advisor_model is None and kv == {"advisor_model": "off"}
    rt.openai.probe_advisor_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_show_in_any_case(kv):
    rt = _admin_rt()
    msg = _cmd("/ai_advisor SHOW")
    await _ai_advisor(rt)(msg)
    rt.openai.probe_advisor_model.assert_not_awaited()
    assert "Advisor: claude-opus-5-5" in msg.reply.await_args.args[0] and kv == {}


# ── startup (bot.build_runtime) ──────────────────────────────────────────────

class _Stop(Exception):
    pass


async def _runtime_client(monkeypatch, saved, **settings):
    """Run build_runtime up to the point the AI client exists and return it."""
    built = []

    class Spy(bot_module.OpenAIClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            built.append(self)

    async def kv_get(db, key):
        return saved.get(key)

    def stop(_):
        raise _Stop

    monkeypatch.setattr(bot_module, "OpenAIClient", Spy)
    monkeypatch.setattr(bot_module, "kv_get", kv_get)
    monkeypatch.setattr(bot_module.Database, "connect", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(bot_module, "set_db", lambda db: None)
    monkeypatch.setattr(bot_module, "apply_schema", AsyncMock())
    monkeypatch.setattr(bot_module, "has_pgvector", AsyncMock(return_value=False))
    monkeypatch.setattr(bot_module, "set_default_prompt", stop)
    s = Settings(telegram_bot_token="123456:ABCdefGhIJKlmNoPQRsTUVwxyZ",
                 openai_api_key="k", anthropic_api_key="k",
                 database_url="postgresql://t/t", **settings)
    with pytest.raises(_Stop):
        await bot_module.build_runtime(s)
    return built[0]


@pytest.mark.asyncio
async def test_startup_uses_the_saved_advisor_over_the_setting(monkeypatch):
    """Survived: build_runtime reading another kv key, ignoring the saved value,
    or swapping resolve_advisor_model's arguments. (resolve_advisor_model was
    tested; the wiring in build_runtime and the 'advisor_model' key that
    /ai_advisor writes were not.)"""
    ai = await _runtime_client(monkeypatch, {"advisor_model": "claude-opus-4-8"},
                               advisor_model="claude-opus-5-5")
    assert ai.advisor_model == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_startup_honours_off_and_the_setting_and_the_caps(monkeypatch):
    ai = await _runtime_client(monkeypatch, {"advisor_model": "off"},
                               advisor_model="claude-opus-5-5")
    assert ai.advisor_model is None
    ai = await _runtime_client(monkeypatch, {}, advisor_model="claude-opus-5-5",
                               advisor_max_tokens=2048, advisor_max_uses=2)
    assert ai.advisor_model == "claude-opus-5-5"
    assert (ai.advisor_max_tokens, ai.advisor_max_uses) == (2048, 2)
    tool = ai._advisor_tool(ai.claude_model)
    assert (tool["max_tokens"], tool["max_uses"]) == (2048, 2)


# ── the brief ────────────────────────────────────────────────────────────────

def test_the_advisor_paragraph_keeps_its_place_and_the_rest_of_its_instructions():
    """Survived: paragraph moved after _IN_CHARACTER (it pushes the
    stay-in-character rule off the end of the brief); 'the reply is still
    yours', the topic list, 'write the reply the length you normally would'
    and the advisor-addressed 'this is a chat message' sentence deleted."""
    from ipedro import capabilities
    from ipedro.capabilities import capability_brief
    on = capability_brief(advisor=True)
    assert on.index(capabilities._ADVISOR) < on.index("Slash commands anyone here can use:")
    assert on.rstrip().endswith(capability_brief().rstrip()[-200:])    # still ends as it did
    for phrase in ("the reply is still yours, in your voice",
                   "money, health or legal",
                   "Write the reply the length you normally would",
                   "this is a chat message, not an essay"):
        assert phrase in capabilities._ADVISOR, phrase


# ── where an admin finds /ai_advisor ────────────────────────────────────────

def test_ai_advisor_is_in_the_manage_catalog_the_admin_help_and_the_docs():
    """Survived the whole suite: the catalog row, the HELP_TEXT_ADMIN line, the
    README row and the docs heading can each vanish or drift (test_command_catalog
    only checks catalog *actions*)."""
    from pathlib import Path

    from ipedro.handlers.basics import HELP_TEXT_ADMIN
    from ipedro.handlers.command_catalog import command_by_slug

    cmd = command_by_slug("ai_advisor")
    assert cmd is not None and cmd.category == "ai_admin"
    assert cmd.name == "/ai_advisor" and cmd.usage.startswith("/ai_advisor show")
    assert "/ai_advisor show|off|<model_id>" in HELP_TEXT_ADMIN
    root = Path(__file__).resolve().parent.parent
    assert "/ai_advisor show | off | <model_id>" in (root / "docs" / "COMMANDS.md").read_text()
    assert "/ai_advisor show" in (root / "README.md").read_text()


def test_env_example_shows_the_defaults_the_code_has():
    """Survived: `#ADVISOR_MODEL=` in .env.example edited to another model
    (test_deploy_files only checks that each setting is *listed*)."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
    s = Settings(telegram_bot_token="t", openai_api_key="k", database_url="postgresql://t/t")
    for key, want in (("ADVISOR_MODEL", s.advisor_model),
                      ("ADVISOR_MAX_TOKENS", s.advisor_max_tokens),
                      ("ADVISOR_MAX_USES", s.advisor_max_uses)):
        got = re.search(rf"^#?{key}=(.*)$", text, re.M).group(1).strip()
        assert got == str(want), key


# ── a refusal is remembered for the pairing that was refused ────────────────

@pytest.mark.asyncio
async def test_a_refusal_is_remembered_for_the_pairing_that_was_refused():
    """Survived (unreachable from chat.py today, which never passes model= with
    advisor=True): the block keyed on self.claude_model instead of the model the
    request used, and the advisor tool built for self.claude_model."""
    c, plain, beta = _client(plain=[_resp(_text("a")), _resp(_text("b"))],
                             beta=[_bad_request()])
    await c.chat(MESSAGES, model="claude-haiku-4-5", max_tokens=50, advisor=True)
    assert c.advisor_available() and not c.advisor_turned_down      # sonnet-5 + opus never tried
    await c.chat(MESSAGES, model="claude-haiku-4-5", max_tokens=50, advisor=True)
    assert len(beta.sent) == 1                                      # haiku + opus not tried again
