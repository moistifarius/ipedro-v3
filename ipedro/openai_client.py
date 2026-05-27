"""Thin wrapper around the modern OpenAI Python SDK.

Centralises model selection, retries and error handling so callers never
talk to the SDK directly. All methods are safe to call even when the API
is misconfigured - they return None / empty defaults and log the error.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, BinaryIO, Sequence

from openai import AsyncOpenAI, APIError
from tenacity import (
    retry, retry_if_exception_type, stop_after_attempt, wait_exponential,
)

log = logging.getLogger(__name__)

_RETRY = dict(
    retry=retry_if_exception_type(APIError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=False,
)


class OpenAIClient:
    """Async wrapper around openai.AsyncOpenAI with sane defaults."""

    def __init__(
        self,
        api_key: str,
        *,
        organization: str | None = None,
        text_model: str = "gpt-4o-mini",
        image_model: str = "gpt-image-1",
        transcription_model: str = "whisper-1",
        embedding_model: str = "text-embedding-3-small",
        embedding_dim: int = 1536,
    ) -> None:
        self._client = AsyncOpenAI(api_key=api_key, organization=organization or None)
        self.text_model = text_model
        self.image_model = image_model
        self.transcription_model = transcription_model
        self.embedding_model = embedding_model
        self.embedding_dim = embedding_dim

    # ----------------------------------------------------------- text
    @retry(**_RETRY)  # type: ignore[arg-type]
    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = None,
        max_tokens: int = 600,
        temperature: float = 1.0,
    ) -> str | None:
        try:
            resp = await self._client.chat.completions.create(
                model=model or self.text_model,
                messages=list(messages),
                max_tokens=max_tokens,
                temperature=temperature,
            )
            choice = resp.choices[0]
            return (choice.message.content or "").strip() or None
        except Exception as exc:
            log.error("OpenAI chat error: %s", exc)
            return None

    async def short_completion(self, prompt: str, *, max_tokens: int = 200) -> str | None:
        return await self.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )

    # ----------------------------------------------------------- embeddings
    @retry(**_RETRY)  # type: ignore[arg-type]
    async def embed(self, text: str) -> list[float] | None:
        try:
            text = text.strip()
            if not text:
                return None
            resp = await self._client.embeddings.create(
                model=self.embedding_model,
                input=text[:8000],
            )
            return list(resp.data[0].embedding)
        except Exception as exc:
            log.warning("OpenAI embedding error: %s", exc)
            return None

    # ----------------------------------------------------------- images
    async def generate_image(self, prompt: str, *, size: str = "1024x1024") -> bytes | None:
        """Return raw PNG bytes for the generated image."""
        try:
            resp = await self._client.images.generate(
                model=self.image_model,
                prompt=prompt,
                size=size,
                n=1,
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

    # ----------------------------------------------------------- audio
    async def transcribe(self, audio: BinaryIO, filename: str = "audio.ogg") -> str | None:
        try:
            audio.seek(0)
            file_tuple = (filename, audio.read())
            resp = await self._client.audio.transcriptions.create(
                model=self.transcription_model,
                file=file_tuple,
            )
            return (getattr(resp, "text", "") or "").strip() or None
        except Exception as exc:
            log.error("OpenAI transcribe error: %s", exc)
            return None

    async def translate_audio(self, audio: BinaryIO, filename: str = "audio.ogg") -> str | None:
        try:
            audio.seek(0)
            file_tuple = (filename, audio.read())
            resp = await self._client.audio.translations.create(
                model=self.transcription_model,
                file=file_tuple,
            )
            return (getattr(resp, "text", "") or "").strip() or None
        except Exception as exc:
            log.error("OpenAI translate error: %s", exc)
            return None
