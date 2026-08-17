"""Multi-provider AI client.

Text completions (`chat`, `short_completion`) route to either Anthropic
(Claude) or OpenAI based on the active provider setting; the provider can
be swapped at runtime via `set_text_provider()`. Embeddings, image
generation, and audio (transcription / translation) always go to OpenAI
because Anthropic has no equivalents.

Callers continue to use `OpenAIClient` as the import — it's now an alias
for `AIClient`. Existing call sites need no changes; the routing is
transparent.

All methods are safe to call even when the relevant provider is
misconfigured — they return None / empty defaults and log the error.
Each call accepts an optional `chat_id` kwarg; when set (and a db ref
has been attached via `attach_usage_db`), the call writes a row to
openai_usage with token counts and a rough USD cost estimate.
"""

from __future__ import annotations

import base64
import inspect
import logging
from typing import Any, BinaryIO, Literal, Sequence

from anthropic import (
    APIConnectionError as AnthropicAPIConnectionError,
    APIError as AnthropicAPIError,
    APITimeoutError as AnthropicAPITimeoutError,
    AsyncAnthropic,
    InternalServerError as AnthropicInternalServerError,
)
from openai import (
    APIConnectionError as OpenAIAPIConnectionError,
    APIError as OpenAIAPIError,
    APITimeoutError as OpenAIAPITimeoutError,
    AsyncOpenAI,
    InternalServerError as OpenAIInternalServerError,
)
from tenacity import (
    retry, retry_if_exception_type, stop_after_attempt, wait_exponential,
)

from ipedro.db.pool import Database

log = logging.getLogger(__name__)

# Only retry TRANSIENT errors (connection drops, timeouts, upstream 5xx).
# Crucially NOT RateLimitError (429): retrying immediately just slams the
# limit again and inflates Anthropic's hit counter. A 429 propagates up
# and the calling feature degrades gracefully (None response).
_OPENAI_RETRY = dict(
    retry=retry_if_exception_type((
        OpenAIAPIConnectionError, OpenAIAPITimeoutError,
        OpenAIInternalServerError,
    )),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=False,
)

_CLAUDE_RETRY = dict(
    retry=retry_if_exception_type((
        AnthropicAPIConnectionError, AnthropicAPITimeoutError,
        AnthropicInternalServerError,
    )),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=False,
)

# Rough per-1k-token USD estimates by model prefix.
_OPENAI_TEXT_PRICE_PER_1K = {
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.01),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "gpt-4.1": (0.002, 0.008),
}
_CLAUDE_TEXT_PRICE_PER_1K = {
    "claude-opus-4-7":   (0.005, 0.025),
    "claude-opus-4-6":   (0.005, 0.025),
    "claude-opus-4-5":   (0.005, 0.025),
    "claude-sonnet-4-6": (0.003, 0.015),
    "claude-sonnet-4-5": (0.003, 0.015),
    "claude-haiku-4-5":  (0.001, 0.005),
}
_EMBED_PRICE_PER_1K = {
    "text-embedding-3-small": 0.00002,
    "text-embedding-3-large": 0.00013,
}
_IMAGE_PRICE = {
    "gpt-image-1": 0.04,
    "dall-e-3": 0.04,
    "dall-e-2": 0.02,
}
_AUDIO_PER_MINUTE = 0.006
# Rough TTS cost estimate for the usage log, charged per 1k input chars.
# Good enough for the /cost rollup; exact pricing varies by model.
_TTS_PER_1K_CHARS = 0.015

TextProvider = Literal["claude", "openai"]


def _openai_text_price(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rate = next(
        (v for k, v in _OPENAI_TEXT_PRICE_PER_1K.items() if model.startswith(k)),
        (0.001, 0.003),
    )
    return (prompt_tokens / 1000) * rate[0] + (completion_tokens / 1000) * rate[1]


def _claude_text_price(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rate = next(
        (v for k, v in _CLAUDE_TEXT_PRICE_PER_1K.items() if model.startswith(k)),
        (0.003, 0.015),
    )
    return (prompt_tokens / 1000) * rate[0] + (completion_tokens / 1000) * rate[1]


def _normalize_for_claude(
    messages: Sequence[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Split system messages from chat messages and make the chat array
    safe for Claude (no leading assistant, no consecutive same-role)."""
    system_parts: list[str] = []
    chat: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            if content:
                system_parts.append(str(content))
            continue
        if role not in ("user", "assistant"):
            continue
        if not chat and role == "assistant":
            # Claude requires the first message to be 'user'; drop a stray leading assistant.
            continue
        if chat and chat[-1]["role"] == role:
            # Merge consecutive same-role messages.
            chat[-1]["content"] = (chat[-1]["content"] or "") + "\n\n" + (content or "")
            continue
        chat.append({"role": role, "content": content or ""})
    system = "\n\n".join(p for p in system_parts if p) or None
    if not chat:
        # Claude requires at least one message — synthesize a noop.
        chat.append({"role": "user", "content": "(continue)"})
    return system, chat


class AIClient:
    """Async multi-provider AI client (Claude for text, OpenAI for the rest)."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        organization: str | None = None,
        anthropic_api_key: str | None = None,
        text_provider: TextProvider | None = None,
        text_model: str = "gpt-4o-mini",
        claude_model: str = "claude-sonnet-4-6",
        # Low-stakes routing: classifiers, judges, one-liners go through
        # cheap_chat/cheap_completion, which use these models regardless
        # of the primary text_provider. ~3x cheaper than Sonnet, with a
        # separate rate-limit quota — so a spike in /catfact or bef
        # judging doesn't eat your /a or /tldr capacity.
        cheap_claude_model: str = "claude-haiku-4-5",
        cheap_openai_model: str = "gpt-4o-mini",
        image_model: str = "gpt-image-1",
        transcription_model: str = "whisper-1",
        embedding_model: str = "text-embedding-3-small",
        embedding_dim: int = 1536,
        tts_model: str = "gpt-4o-mini-tts",
        tts_voice: str = "onyx",
    ) -> None:
        self._openai = (
            AsyncOpenAI(api_key=api_key, organization=organization or None)
            if api_key else None
        )
        self._anthropic = (
            AsyncAnthropic(api_key=anthropic_api_key)
            if anthropic_api_key else None
        )
        # Auto-default: claude if we have the key, else openai.
        if text_provider is None:
            text_provider = "claude" if anthropic_api_key else "openai"
        # Honor the explicit choice but degrade if the key is missing.
        if text_provider == "claude" and not anthropic_api_key:
            log.warning(
                "text_provider=claude requested but no anthropic_api_key; "
                "falling back to openai."
            )
            text_provider = "openai"
        self._text_provider: TextProvider = text_provider
        self.text_model = text_model
        self.claude_model = claude_model
        self.cheap_claude_model = cheap_claude_model
        self.cheap_openai_model = cheap_openai_model
        self.image_model = image_model
        self.transcription_model = transcription_model
        self.embedding_model = embedding_model
        self.embedding_dim = embedding_dim
        self.tts_model = tts_model
        self.tts_voice = tts_voice
        self._usage_db: Database | None = None

    # back-compat property alias so callers can still poke _client.* in tests
    @property
    def _client(self):
        return self._openai

    # ----------------------------------------------------------- provider switching
    @property
    def text_provider(self) -> TextProvider:
        return self._text_provider

    def set_text_provider(self, provider: TextProvider) -> None:
        if provider not in ("claude", "openai"):
            raise ValueError(f"invalid text_provider: {provider!r}")
        if provider == "claude" and self._anthropic is None:
            raise ValueError("anthropic SDK not configured (missing ANTHROPIC_API_KEY)")
        if provider == "openai" and self._openai is None:
            raise ValueError("openai SDK not configured (missing OPENAI_API_KEY)")
        self._text_provider = provider

    def set_claude_model(self, model: str) -> None:
        self.claude_model = model

    def set_openai_text_model(self, model: str) -> None:
        self.text_model = model

    # ----------------------------------------------------------- usage logging
    def attach_usage_db(self, db: Database) -> None:
        self._usage_db = db

    async def _log_usage(
        self, *, kind: str, model: str | None, chat_id: int | None,
        prompt_tokens: int | None = None, completion_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        if self._usage_db is None:
            return
        try:
            total = None
            if prompt_tokens is not None or completion_tokens is not None:
                total = (prompt_tokens or 0) + (completion_tokens or 0)
            await self._usage_db.execute(
                "INSERT INTO openai_usage (chat_id, kind, model, "
                " prompt_tokens, completion_tokens, total_tokens, cost_usd) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                chat_id, kind, model,
                prompt_tokens, completion_tokens, total, cost_usd,
            )
        except Exception as exc:
            log.debug("Usage log write failed: %s", exc)

    # ----------------------------------------------------------- text (routed)
    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = None,
        max_tokens: int = 600,
        temperature: float = 1.0,
        chat_id: int | None = None,
    ) -> str | None:
        """Route a chat completion to the active text provider.

        The inner provider methods are decorated with tenacity; on
        exhausted retries tenacity raises a RetryError (and the inner
        try/except blocks re-raise the SDK's APIError so retries
        actually fire). Convert any of those to None here so callers
        get the same graceful degradation regardless of provider.
        """
        try:
            if self._text_provider == "claude" and self._anthropic is not None:
                return await self._chat_claude(
                    messages, max_tokens=max_tokens,
                    temperature=temperature, chat_id=chat_id,
                )
            return await self._chat_openai(
                messages, model=model, max_tokens=max_tokens,
                temperature=temperature, chat_id=chat_id,
            )
        except Exception as exc:
            log.error("chat() final failure (%s): %s", self._text_provider, exc)
            return None

    async def short_completion(
        self, prompt: str, *, max_tokens: int = 200,
        chat_id: int | None = None,
    ) -> str | None:
        return await self.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=max_tokens, chat_id=chat_id,
        )

    # --------------- cheap routing (classifiers / judges / one-liners)
    async def cheap_chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        max_tokens: int = 200,
        temperature: float = 1.0,
        chat_id: int | None = None,
    ) -> str | None:
        """Like ``chat`` but forces the configured cheap model.

        Use for low-stakes calls — classifiers, scoring, judges, short
        one-liners — where Sonnet quality is overkill. Haiku is ~3x
        cheaper and has its own rate-limit quota, so a spike in the
        cheap path can't eat the quota that /a, /tldr, and main chat
        replies depend on. The cheap provider is decoupled from
        ``text_provider``: it always runs Claude Haiku when an
        Anthropic key is configured, else gpt-4o-mini.
        """
        try:
            if self._anthropic is not None:
                return await self._chat_claude(
                    messages,
                    model=self.cheap_claude_model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    chat_id=chat_id,
                )
            return await self._chat_openai(
                messages,
                model=self.cheap_openai_model,
                max_tokens=max_tokens,
                temperature=temperature,
                chat_id=chat_id,
            )
        except Exception as exc:
            log.error("cheap_chat() failure: %s", exc)
            return None

    async def cheap_completion(
        self, prompt: str, *, max_tokens: int = 200,
        chat_id: int | None = None, temperature: float = 1.0,
    ) -> str | None:
        return await self.cheap_chat(
            [{"role": "user", "content": prompt}],
            max_tokens=max_tokens, temperature=temperature, chat_id=chat_id,
        )

    @retry(**_OPENAI_RETRY)  # type: ignore[arg-type]
    async def _chat_openai(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None,
        max_tokens: int,
        temperature: float,
        chat_id: int | None,
    ) -> str | None:
        if self._openai is None:
            log.error("OpenAI chat requested but no openai_api_key.")
            return None
        m = model or self.text_model
        try:
            resp = await self._openai.chat.completions.create(
                model=m,
                messages=list(messages),
                max_tokens=max_tokens,
                temperature=temperature,
            )
            choice = resp.choices[0]
            usage = getattr(resp, "usage", None)
            pt = getattr(usage, "prompt_tokens", 0) or 0
            ct = getattr(usage, "completion_tokens", 0) or 0
            await self._log_usage(
                kind="chat", model=m, chat_id=chat_id,
                prompt_tokens=pt, completion_tokens=ct,
                cost_usd=_openai_text_price(m, pt, ct),
            )
            return (choice.message.content or "").strip() or None
        except OpenAIAPIError:
            # Let tenacity's @retry see this and retry; the wrapping
            # chat() catches whatever survives exhaustion.
            raise
        except Exception as exc:
            log.error("OpenAI chat error: %s", exc)
            return None

    @retry(**_CLAUDE_RETRY)  # type: ignore[arg-type]
    async def _chat_claude(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = None,
        max_tokens: int,
        temperature: float,
        chat_id: int | None,
    ) -> str | None:
        if self._anthropic is None:
            log.error("Claude chat requested but no anthropic_api_key.")
            return None
        m = model or self.claude_model
        system, chat_messages = _normalize_for_claude(messages)
        kwargs: dict[str, Any] = {
            "model": m,
            "max_tokens": max_tokens,
            "messages": chat_messages,
        }
        if system:
            kwargs["system"] = system
        # Opus 4.7 rejects sampling parameters; skip them on that model.
        if not m.startswith("claude-opus-4-7"):
            kwargs["temperature"] = max(0.0, min(1.0, temperature))
        try:
            resp = await self._anthropic.messages.create(**kwargs)
            text_parts = [
                block.text for block in resp.content
                if getattr(block, "type", None) == "text"
            ]
            usage = getattr(resp, "usage", None)
            pt = getattr(usage, "input_tokens", 0) or 0
            ct = getattr(usage, "output_tokens", 0) or 0
            await self._log_usage(
                kind="chat", model=m, chat_id=chat_id,
                prompt_tokens=pt, completion_tokens=ct,
                cost_usd=_claude_text_price(m, pt, ct),
            )
            out = "\n".join(text_parts).strip()
            return out or None
        except AnthropicAPIError:
            raise
        except Exception as exc:
            log.error("Claude chat error: %s", exc)
            return None

    # ----------------------------------------------------------- embeddings (OpenAI only)
    async def embed(
        self, text: str, *, chat_id: int | None = None,
    ) -> list[float] | None:
        """Embed ``text``; never raises.

        Transient failures retry inside ``_embed_with_retry``; whatever
        survives exhaustion (tenacity's RetryError — the retry config has
        reraise=False — or a non-retryable APIError like a 429) is
        converted to None here so callers (memory/store.py record paths)
        degrade gracefully.
        """
        try:
            return await self._embed_with_retry(text, chat_id=chat_id)
        except Exception as exc:
            log.warning("Embedding final failure: %s", exc)
            return None

    @retry(**_OPENAI_RETRY)  # type: ignore[arg-type]
    async def _embed_with_retry(
        self, text: str, *, chat_id: int | None = None,
    ) -> list[float] | None:
        if self._openai is None:
            log.warning("Embed requested but no openai_api_key.")
            return None
        try:
            text = text.strip()
            if not text:
                return None
            resp = await self._openai.embeddings.create(
                model=self.embedding_model,
                input=text[:8000],
            )
            usage = getattr(resp, "usage", None)
            pt = getattr(usage, "prompt_tokens", 0) or 0
            rate = _EMBED_PRICE_PER_1K.get(self.embedding_model, 0.00002)
            await self._log_usage(
                kind="embed", model=self.embedding_model, chat_id=chat_id,
                prompt_tokens=pt, cost_usd=(pt / 1000) * rate,
            )
            return list(resp.data[0].embedding)
        except OpenAIAPIError:
            # Let tenacity's @retry see this and retry; the wrapping
            # embed() catches whatever survives exhaustion.
            raise
        except Exception as exc:
            log.warning("OpenAI embedding error: %s", exc)
            return None

    # ----------------------------------------------------------- images (OpenAI only)
    async def generate_image(
        self, prompt: str, *, size: str = "1024x1024",
        chat_id: int | None = None,
    ) -> bytes | None:
        """Return raw PNG bytes for the generated image."""
        if self._openai is None:
            log.error("Image gen requested but no openai_api_key.")
            return None
        try:
            resp = await self._openai.images.generate(
                model=self.image_model,
                prompt=prompt,
                size=size,
                n=1,
            )
            await self._log_usage(
                kind="image", model=self.image_model, chat_id=chat_id,
                cost_usd=_IMAGE_PRICE.get(self.image_model, 0.04),
            )
            data = resp.data[0]
            if getattr(data, "b64_json", None):
                return base64.b64decode(data.b64_json)
            if getattr(data, "url", None):
                import httpx

                async with httpx.AsyncClient(timeout=60) as http:
                    r = await http.get(data.url)
                    r.raise_for_status()
                    return r.content
            return None
        except Exception as exc:
            log.error("OpenAI image error: %s", exc)
            return None

    # ----------------------------------------------------------- audio (OpenAI only)
    async def transcribe(
        self, audio: BinaryIO, filename: str = "audio.ogg",
        chat_id: int | None = None,
    ) -> str | None:
        if self._openai is None:
            log.error("Transcribe requested but no openai_api_key.")
            return None
        try:
            audio.seek(0)
            file_tuple = (filename, audio.read())
            resp = await self._openai.audio.transcriptions.create(
                model=self.transcription_model,
                file=file_tuple,
            )
            await self._log_usage(
                kind="transcribe", model=self.transcription_model,
                chat_id=chat_id, cost_usd=_AUDIO_PER_MINUTE,
            )
            return (getattr(resp, "text", "") or "").strip() or None
        except Exception as exc:
            log.error("OpenAI transcribe error: %s", exc)
            return None

    async def translate_audio(
        self, audio: BinaryIO, filename: str = "audio.ogg",
        chat_id: int | None = None,
    ) -> str | None:
        if self._openai is None:
            log.error("Translate requested but no openai_api_key.")
            return None
        try:
            audio.seek(0)
            file_tuple = (filename, audio.read())
            resp = await self._openai.audio.translations.create(
                model=self.transcription_model,
                file=file_tuple,
            )
            await self._log_usage(
                kind="translate", model=self.transcription_model,
                chat_id=chat_id, cost_usd=_AUDIO_PER_MINUTE,
            )
            return (getattr(resp, "text", "") or "").strip() or None
        except Exception as exc:
            log.error("OpenAI translate error: %s", exc)
            return None

    async def text_to_speech(
        self, text: str, *, voice: str | None = None, fmt: str = "mp3",
        chat_id: int | None = None,
    ) -> bytes | None:
        """Synthesize speech and return the raw audio bytes (default mp3).

        Returns None if the OpenAI key is absent, the text is empty, or
        the call errors — callers fall back to a text broadcast.
        """
        if self._openai is None:
            log.error("TTS requested but no openai_api_key.")
            return None
        text = (text or "").strip()
        if not text:
            return None
        text = text[:4000]  # API input cap; keep transmissions short anyway
        try:
            resp = await self._openai.audio.speech.create(
                model=self.tts_model,
                voice=voice or self.tts_voice,
                input=text,
                response_format=fmt,
            )
            data = await _read_binary_response(resp)
            if not data:
                return None
            await self._log_usage(
                kind="tts", model=self.tts_model, chat_id=chat_id,
                cost_usd=(len(text) / 1000.0) * _TTS_PER_1K_CHARS,
            )
            return data
        except Exception as exc:
            log.error("OpenAI TTS error: %s", exc)
            return None


async def _read_binary_response(resp: Any) -> bytes | None:
    """Pull bytes out of the OpenAI speech response across SDK versions.

    Newer SDKs return an object exposing ``.content`` (already-read bytes);
    some expose ``.read()``/``.aread()``. Try them in order.
    """
    content = getattr(resp, "content", None)
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    for attr in ("aread", "read"):
        fn = getattr(resp, attr, None)
        if fn is None:
            continue
        try:
            out = fn()
            if inspect.isawaitable(out):
                out = await out
            if isinstance(out, (bytes, bytearray)):
                return bytes(out)
        except Exception:  # pragma: no cover - defensive
            continue
    return None


# Back-compat alias — historical name used everywhere in the codebase.
OpenAIClient = AIClient
