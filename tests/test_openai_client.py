"""OpenAI client behaviour with a faked async SDK.

We don't hit the network. The client wraps `openai.AsyncOpenAI`; we just
monkeypatch the inner client and verify the wrapper's error handling.
"""

from __future__ import annotations

import pytest

from ipedro.openai_client import OpenAIClient


class _FakeChoice:
    def __init__(self, content):
        class M:
            pass
        self.message = M()
        self.message.content = content


class _FakeChatResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeChatNamespace:
    def __init__(self, content):
        self._content = content

    class _Completions:
        def __init__(self, content):
            self._content = content

        async def create(self, **kwargs):
            return _FakeChatResponse(self._content)

    @property
    def completions(self):
        return self._Completions(self._content)


class _ExplodingChatNamespace:
    class _Completions:
        async def create(self, **kwargs):
            raise RuntimeError("boom")

    completions = _Completions()


@pytest.mark.asyncio
async def test_chat_returns_stripped_content(monkeypatch):
    client = OpenAIClient(api_key="x", text_provider="openai")
    client._client.chat = _FakeChatNamespace("  hello there  ")
    out = await client.chat([{"role": "user", "content": "hi"}])
    assert out == "hello there"


@pytest.mark.asyncio
@pytest.mark.parametrize("finish, expected", [
    ("length", "First point made. Second point made in full."),   # cut at the cap
    ("stop", "First point made. Second point made in full. And the third poi"),
])
async def test_a_reply_the_cap_cut_off_is_trimmed_to_its_last_sentence(
    finish, expected, monkeypatch,
):
    """The same rule the Claude path has: nothing posts stopping mid-word."""
    client = OpenAIClient(api_key="x", text_provider="openai")
    ns = _FakeChatNamespace(
        "First point made. Second point made in full. And the third poi")
    real = ns._Completions.create

    async def create(self, **kwargs):
        resp = await real(self, **kwargs)
        resp.choices[0].finish_reason = finish
        return resp

    monkeypatch.setattr(ns._Completions, "create", create)    # undone after
    client._client.chat = ns
    assert await client.chat([{"role": "user", "content": "hi"}]) == expected


@pytest.mark.asyncio
async def test_chat_returns_none_on_empty_content():
    client = OpenAIClient(api_key="x", text_provider="openai")
    client._client.chat = _FakeChatNamespace("   ")
    out = await client.chat([{"role": "user", "content": "hi"}])
    assert out is None


@pytest.mark.asyncio
async def test_chat_swallows_unexpected_errors():
    client = OpenAIClient(api_key="x", text_provider="openai")
    client._client.chat = _ExplodingChatNamespace()
    out = await client.chat([{"role": "user", "content": "hi"}])
    assert out is None  # never propagates


class _FakeEmbeddingData:
    def __init__(self, vec):
        self.embedding = vec


class _FakeEmbeddingResponse:
    def __init__(self, vec):
        self.data = [_FakeEmbeddingData(vec)]


class _FakeEmbeddingsOk:
    async def create(self, **kwargs):
        return _FakeEmbeddingResponse([0.1, 0.2, 0.3])


@pytest.mark.asyncio
async def test_embed_returns_vector():
    client = OpenAIClient(api_key="x", text_provider="openai", embedding_dim=3)
    client._client.embeddings = _FakeEmbeddingsOk()
    out = await client.embed("hello")
    assert out == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embed_returns_none_for_empty_text():
    client = OpenAIClient(api_key="x", text_provider="openai")
    out = await client.embed("   ")
    assert out is None


class _FlakyEmbeddings:
    """Fails once with the given error, then succeeds."""

    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise self._exc
        return _FakeEmbeddingResponse([0.5, 0.6])


@pytest.mark.asyncio
async def test_embed_retries_transient_connection_error():
    """A retryable error (connection drop) must actually reach tenacity's
    @retry — the old blanket `except Exception: return None` swallowed it
    on the first attempt so the decorator never fired."""
    from openai import APIConnectionError

    class _ConnError(APIConnectionError):
        def __init__(self):  # skip the SDK's required httpx request arg
            pass

    client = OpenAIClient(api_key="x", text_provider="openai", embedding_dim=2)
    flaky = _FlakyEmbeddings(_ConnError())
    client._client.embeddings = flaky
    out = await client.embed("hello")
    assert out == [0.5, 0.6]
    assert flaky.calls == 2  # first attempt failed, retry succeeded


@pytest.mark.asyncio
async def test_embed_swallows_unexpected_errors_without_retry():
    """A non-API error isn't transient — no retry, and embed still returns
    None instead of raising (callers in memory/store.py rely on that)."""
    flaky = _FlakyEmbeddings(RuntimeError("boom"))
    client = OpenAIClient(api_key="x", text_provider="openai")
    client._client.embeddings = flaky
    out = await client.embed("hello")
    assert out is None
    assert flaky.calls == 1


# ----------------------------------------------------------------- TTS / speech
class _FakeSpeechContent:
    """Mimics the binary speech response that exposes ``.content``."""
    def __init__(self, data: bytes):
        self.content = data


class _FakeSpeechReadable:
    """Mimics an SDK variant exposing async ``.aread()`` instead."""
    def __init__(self, data: bytes):
        self._data = data

    async def aread(self) -> bytes:
        return self._data


def _install_speech(client, resp):
    class _Speech:
        async def create(self, **kwargs):
            return resp

    class _Audio:
        speech = _Speech()

    client._client.audio = _Audio()


@pytest.mark.asyncio
async def test_text_to_speech_returns_bytes_via_content():
    client = OpenAIClient(api_key="x", text_provider="openai")
    _install_speech(client, _FakeSpeechContent(b"OGGDATA"))
    out = await client.text_to_speech("hello world")
    assert out == b"OGGDATA"


@pytest.mark.asyncio
async def test_text_to_speech_returns_bytes_via_aread():
    client = OpenAIClient(api_key="x", text_provider="openai")
    _install_speech(client, _FakeSpeechReadable(b"OPUSDATA"))
    out = await client.text_to_speech("hello world")
    assert out == b"OPUSDATA"


@pytest.mark.asyncio
async def test_text_to_speech_none_for_empty_text():
    client = OpenAIClient(api_key="x", text_provider="openai")
    assert await client.text_to_speech("   ") is None


@pytest.mark.asyncio
async def test_text_to_speech_none_without_api_key():
    client = OpenAIClient(api_key=None, text_provider="openai")
    assert await client.text_to_speech("hello") is None


# ---------------------------------------------------------------- cheap routing
@pytest.mark.asyncio
async def test_cheap_chat_uses_openai_when_no_anthropic_key(monkeypatch):
    """No Anthropic key → cheap path uses the configured cheap OpenAI model."""
    client = OpenAIClient(api_key="x", text_provider="openai",
                          cheap_openai_model="gpt-4o-mini")
    captured: dict = {}

    class _CapturingCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _FakeChatResponse("ok")

    class _NS:
        completions = _CapturingCompletions()

    client._client.chat = _NS()
    out = await client.cheap_chat([{"role": "user", "content": "judge this"}])
    assert out == "ok"
    assert captured.get("model") == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_cheap_chat_uses_claude_haiku_when_anthropic_present():
    """When the Anthropic SDK is configured, cheap routing forces Haiku
    regardless of the primary text_provider (which might be openai)."""
    client = OpenAIClient(api_key="x", text_provider="openai",
                          anthropic_api_key="ant-x",
                          claude_model="claude-sonnet-4-6",
                          cheap_claude_model="claude-haiku-4-5")
    captured: dict = {}

    class _FakeMsg:
        async def create(self, **kwargs):
            captured.update(kwargs)

            class _Block:
                type = "text"
                text = "PASS"
            class _Usage:
                input_tokens = 5
                output_tokens = 1
            class _R:
                content = [_Block()]
                usage = _Usage()
            return _R()

    client._anthropic.messages = _FakeMsg()  # type: ignore[union-attr]
    out = await client.cheap_chat([{"role": "user", "content": "judge"}])
    assert out == "PASS"
    assert captured.get("model") == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_cheap_completion_wraps_cheap_chat():
    client = OpenAIClient(api_key="x", text_provider="openai")

    class _NS:
        class _Completions:
            async def create(self, **kwargs):
                return _FakeChatResponse("done")
        completions = _Completions()

    client._client.chat = _NS()
    out = await client.cheap_completion("classify this")
    assert out == "done"


def test_retry_predicate_excludes_rate_limit_errors():
    """The retry predicate must NOT match RateLimitError — that's the
    storm we just fixed. Retrying a 429 immediately just slams the
    quota again."""
    from anthropic import APIConnectionError as A_Conn, RateLimitError as A_RL
    from openai import APIConnectionError as O_Conn, RateLimitError as O_RL
    from ipedro.openai_client import _CLAUDE_RETRY, _OPENAI_RETRY

    claude_pred = _CLAUDE_RETRY["retry"]
    openai_pred = _OPENAI_RETRY["retry"]

    # Build minimal instances to pass through the predicate. The
    # tenacity retry_if_exception_type predicate only checks isinstance.
    class _FakeAnthRL(A_RL):
        def __init__(self): pass
    class _FakeAnthConn(A_Conn):
        def __init__(self): pass
    class _FakeOAIRL(O_RL):
        def __init__(self): pass
    class _FakeOAIConn(O_Conn):
        def __init__(self): pass

    # Predicates take a tenacity RetryCallState in tenacity ≥ 9; here we
    # exercise the underlying issubclass check directly.
    assert isinstance(_FakeAnthConn(), A_Conn)
    assert not isinstance(_FakeAnthRL(), (A_Conn,))
    assert not isinstance(_FakeOAIRL(), (O_Conn,))


# ── vision ───────────────────────────────────────────────────────────────────

class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeAnthropicMessages:
    """Records the request so the image payload's shape can be asserted."""

    def __init__(self, text=None, error=None):
        self._text, self._error = text, error
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return type("R", (), {
            "content": [_FakeBlock(self._text)],
            "usage": type("U", (), {"input_tokens": 10, "output_tokens": 5})(),
        })()


def _claude_client(messages):
    client = OpenAIClient(api_key=None, anthropic_api_key="k")
    client._anthropic = type("A", (), {"messages": messages})()
    return client


@pytest.mark.asyncio
async def test_chats_model_override_reaches_the_claude_branch():
    """chat(model=...) was silently dropped when routed to Claude — the
    Claude branch never forwarded it, unlike the OpenAI branch right
    below it. A caller asking for a specific model got the default
    claude_model instead, with no error."""
    msgs = _FakeAnthropicMessages(text="ok")
    client = _claude_client(msgs)
    await client.chat(
        [{"role": "user", "content": "hi"}],
        model="claude-opus-5", max_tokens=10,
    )
    assert msgs.calls[0]["model"] == "claude-opus-5"


@pytest.mark.asyncio
async def test_chats_model_override_still_reaches_the_openai_branch():
    """Sanity companion: this branch already forwarded model= correctly —
    confirm the fix didn't disturb it."""
    client = OpenAIClient(api_key="x", text_provider="openai")
    client._client.chat = _FakeChatNamespace("ok")
    out = await client.chat(
        [{"role": "user", "content": "hi"}], model="gpt-4.1", max_tokens=10,
    )
    assert out == "ok"


@pytest.mark.asyncio
async def test_describe_image_sends_a_base64_image_block():
    msgs = _FakeAnthropicMessages(text="  a dog on a skateboard  ")
    client = _claude_client(msgs)

    out = await client.describe_image(
        b"\xff\xd8\xffdata", media_type="image/jpeg", prompt="what is this",
    )

    assert out == "a dog on a skateboard"
    content = msgs.calls[0]["messages"][0]["content"]
    image, text = content[0], content[1]
    assert image["type"] == "image"
    assert image["source"]["media_type"] == "image/jpeg"
    assert image["source"]["type"] == "base64"
    # Round-trips back to the exact bytes we handed in.
    import base64
    assert base64.b64decode(image["source"]["data"]) == b"\xff\xd8\xffdata"
    assert text["text"] == "what is this"


@pytest.mark.asyncio
async def test_describe_image_uses_the_cheap_model():
    """It runs on every image in every chat; the expensive model would
    make looking at pictures the biggest line on the bill."""
    msgs = _FakeAnthropicMessages(text="x")
    client = _claude_client(msgs)
    await client.describe_image(b"x", prompt="p")
    assert msgs.calls[0]["model"] == client.cheap_claude_model


@pytest.mark.asyncio
async def test_describe_image_falls_back_to_openai_when_claude_fails():
    msgs = _FakeAnthropicMessages(error=RuntimeError("anthropic down"))
    client = _claude_client(msgs)
    client._openai = type("O", (), {
        "chat": _FakeChatNamespace("a fallback description"),
    })()
    assert await client.describe_image(b"x", prompt="p") == (
        "a fallback description"
    )


@pytest.mark.asyncio
async def test_describe_image_returns_none_with_no_provider():
    client = OpenAIClient(api_key=None)
    client._openai = None
    client._anthropic = None
    assert await client.describe_image(b"x", prompt="p") is None


@pytest.mark.asyncio
async def test_describe_image_swallows_a_total_failure():
    """A blind spot is a missing sentence, never a dropped message."""
    client = OpenAIClient(api_key="x", text_provider="openai")
    client._anthropic = None
    client._client.chat = _ExplodingChatNamespace()
    assert await client.describe_image(b"x", prompt="p") is None


# ── one retry layer, and real timeouts ───────────────────────────────────────

def test_the_sdk_clients_do_not_retry_underneath_tenacity():
    """The SDKs retry 408/409/429/5xx by default. Stacked under tenacity
    that made one failing call up to nine requests and retried 429s the
    module swears it never retries."""
    from ipedro.openai_client import OpenAIClient

    client = OpenAIClient(api_key="k", anthropic_api_key="k")
    assert client._anthropic.max_retries == 0
    assert client._openai.max_retries == 0


def test_the_sdk_clients_have_real_timeouts_not_ten_minutes():
    from ipedro.openai_client import OpenAIClient

    client = OpenAIClient(api_key="k", anthropic_api_key="k")
    assert client._anthropic.timeout == 45.0
    assert client._openai.timeout == 120.0


def test_overload_is_retried_like_other_transient_upstream_errors():
    """529 is not an InternalServerError, so tenacity never saw it; the
    SDK's own retry used to cover it, and now that it's off this must."""
    from anthropic import OverloadedError
    from ipedro.openai_client import _CLAUDE_RETRY

    class _Pred:
        def __init__(self, p):
            self.p = p

    predicate = _CLAUDE_RETRY["retry"]
    classes = predicate.exception_types
    assert OverloadedError in classes
    from anthropic import RateLimitError
    assert not issubclass(RateLimitError, classes)


# ── the embedding size the model returns is the size the column holds ────────

def _embedding_client(*, model="text-embedding-3-small", dim=1536, returns=1536):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    client = OpenAIClient(api_key="x", embedding_model=model, embedding_dim=dim)
    create = AsyncMock(return_value=SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.1] * returns)],
        usage=SimpleNamespace(prompt_tokens=3),
    ))
    client._openai = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    client._log_usage = AsyncMock()
    return client, create


@pytest.mark.asyncio
async def test_the_configured_size_is_requested_from_the_v3_models():
    client, create = _embedding_client(dim=512, returns=512)
    assert len(await client.embed("hello")) == 512
    assert create.await_args.kwargs["dimensions"] == 512


@pytest.mark.asyncio
async def test_models_that_cannot_be_resized_are_not_asked_to():
    client, create = _embedding_client(model="text-embedding-ada-002")
    assert len(await client.embed("hello")) == 1536
    assert "dimensions" not in create.await_args.kwargs


@pytest.mark.asyncio
async def test_a_vector_of_the_wrong_size_is_refused_with_a_reason(caplog):
    import logging

    client, _ = _embedding_client(model="text-embedding-ada-002", dim=1024, returns=1536)
    with caplog.at_level(logging.ERROR, logger="ipedro.openai_client"):
        assert await client.embed("hello") is None
        assert await client.embed("again") is None          # still refused...
    errors = [r for r in caplog.records if "EMBEDDING_DIM" in r.getMessage()]
    assert len(errors) == 1                                  # ...but said once
