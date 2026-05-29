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
