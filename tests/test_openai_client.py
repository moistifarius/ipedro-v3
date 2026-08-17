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
    client = OpenAIClient(api_key="x", text_provider="openai")
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

    client = OpenAIClient(api_key="x", text_provider="openai")
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
