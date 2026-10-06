"""/ai_model asks Anthropic before saving a Claude model id.

A model id that Anthropic rejects makes every reply a swallowed 400 (the
bot just stops answering), and the saved setting survives a restart. So
the command probes first and refuses a model that can't answer.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import pytest

from ipedro.handlers import admin
from ipedro.handlers.admin import build_router
from ipedro.openai_client import OpenAIClient


def _handler(rt):
    router = build_router(rt)
    return next(h.callback for h in router.observers["message"].handlers
                if h.callback.__name__ == "ai_model")


def _msg(text):
    return SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private", title=None),
        from_user=SimpleNamespace(id=7, is_bot=False, username="admin"),
        text=text, reply=AsyncMock(),
    )


def _rt(probe=None):
    openai = SimpleNamespace(
        text_provider="claude", claude_model="claude-sonnet-5", text_model="gpt-4o-mini",
        probe_claude_model=AsyncMock(return_value=probe),
        set_claude_model=AsyncMock(), set_openai_text_model=AsyncMock(),
    )
    # set_* are plain methods on the real client
    openai.set_claude_model = lambda m: setattr(openai, "claude_model", m)
    openai.set_openai_text_model = lambda m: setattr(openai, "text_model", m)
    return SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset({7})),
        openai=openai, db=SimpleNamespace(),
    )


@pytest.fixture
def kv(monkeypatch):
    saved = {}

    async def kv_set(db, key, value):
        saved[key] = value

    monkeypatch.setattr(admin, "kv_set", kv_set)
    return saved


@pytest.mark.asyncio
async def test_a_model_that_answers_is_saved(kv):
    rt = _rt(probe=None)
    msg = _msg("/ai_model claude claude-opus-5-5")
    await _handler(rt)(msg)
    rt.openai.probe_claude_model.assert_awaited_once_with("claude-opus-5-5")
    assert rt.openai.claude_model == "claude-opus-5-5"
    assert kv == {"claude_text_model": "claude-opus-5-5"}
    assert "now claude-opus-5-5" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_a_model_anthropic_rejects_changes_nothing(kv):
    rt = _rt(probe="400 invalid_request_error: model: claude-opus-9 not found")
    msg = _msg("/ai_model claude claude-opus-9")
    await _handler(rt)(msg)
    assert rt.openai.claude_model == "claude-sonnet-5"        # unchanged
    assert kv == {}                                           # nothing persisted
    said = msg.reply.await_args.args[0]
    assert "rejected claude-opus-9" in said and "nothing changed" in said
    assert "not found" in said


@pytest.mark.asyncio
async def test_the_bare_form_probes_when_the_active_provider_is_claude(kv):
    rt = _rt(probe="nope")
    await _handler(rt)(_msg("/ai_model claude-opus-9"))
    rt.openai.probe_claude_model.assert_awaited_once()
    assert kv == {}


@pytest.mark.asyncio
async def test_openai_models_are_not_probed(kv):
    rt = _rt(probe="would refuse")
    msg = _msg("/ai_model openai gpt-4.1")
    await _handler(rt)(msg)
    rt.openai.probe_claude_model.assert_not_awaited()
    assert kv == {"openai_text_model": "gpt-4.1"}


# ── the probe itself ─────────────────────────────────────────────────────────

def _client(create):
    client = OpenAIClient(api_key=None, anthropic_api_key="k")
    client._anthropic = SimpleNamespace(messages=SimpleNamespace(create=create))
    return client


@pytest.mark.asyncio
async def test_probe_sends_the_same_shape_chat_would():
    seen = {}

    async def create(**kw):
        seen.update(kw)
        return SimpleNamespace(content=[], usage=None)

    assert await _client(create).probe_claude_model("claude-sonnet-5-5") is None
    # built by _claude_kwargs, so the 5.5 thinking rule is in force
    assert seen["thinking"] == {"type": "between_tools"}
    assert seen["model"] == "claude-sonnet-5-5"
    assert seen["max_tokens"] == 8


@pytest.mark.asyncio
async def test_probe_reports_the_api_error_text():
    async def create(**kw):
        raise anthropic.APIError("model: nope not found", request=SimpleNamespace(), body=None)

    problem = await _client(create).probe_claude_model("nope")
    assert problem and "not found" in problem


@pytest.mark.asyncio
async def test_probe_without_an_anthropic_client_has_nothing_to_ask():
    client = OpenAIClient(api_key=None)
    assert client._anthropic is None
    assert await client.probe_claude_model("anything") is None
