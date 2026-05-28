"""Thin wrapper around the modern OpenAI Python SDK.

Centralises model selection, retries and error handling so callers never
talk to the SDK directly. All methods are safe to call even when the API
is misconfigured - they return None / empty defaults and log the error.

Calls accept an optional `chat_id` kwarg; when set (and a db ref has been
attached via `attach_usage_db`), each call writes a row to openai_usage
with token counts and a rough USD cost estimate.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, BinaryIO, Sequence

from openai import AsyncOpenAI, APIError
from tenacity import (
    retry, retry_if_exception_type, stop_after_attempt, wait_exponential,
)

from ipedro.db.pool import Database

log = logging.getLogger(__name__)

_RETRY = dict(
    retry=retry_if_exception_type(APIError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=False,
)

# Rough per-1k-token USD estimates by model prefix. Order matters: longest
# prefix match wins. Costs change all the time; this is best-effort.
_TEXT_PRICE_PER_1K = {
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.01),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "gpt-4.1": (0.002, 0.008),
}
_EMBED_PRICE_PER_1K = {
    "text-embedding-3-small": 0.00002,
    "text-embedding-3-large": 0.00013,
}
_IMAGE_PRICE = {  # per image at default size
    "gpt-image-1": 0.04,
    "dall-e-3": 0.04,
    "dall-e-2": 0.02,
}
_AUDIO_PER_MINUTE = 0.006  # whisper-1


def _text_price(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rate = next(
        (v for k, v in _TEXT_PRICE_PER_1K.items() if model.startswith(k)),
        (0.001, 0.003),  # safe fallback for unknown text models
    )
    return (prompt_tokens / 1000) * rate[0] + (completion_tokens / 1000) * rate[1]


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
        self._usage_db: Database | None = None

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

    # ----------------------------------------------------------- text
    @retry(**_RETRY)  # type: ignore[arg-type]
    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = None,
        max_tokens: int = 600,
        temperature: float = 1.0,
        chat_id: int | None = None,
    ) -> str | None:
        m = model or self.text_model
        try:
            resp = await self._client.chat.completions.create(
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
                cost_usd=_text_price(m, pt, ct),
            )
            return (choice.message.content or "").strip() or None
        except Exception as exc:
            log.error("OpenAI chat error: %s", exc)
            return None

    async def short_completion(
        self, prompt: str, *, max_tokens: int = 200,
        chat_id: int | None = None,
    ) -> str | None:
        return await self.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=max_tokens, chat_id=chat_id,
        )

    # ----------------------------------------------------------- embeddings
    @retry(**_RETRY)  # type: ignore[arg-type]
    async def embed(
        self, text: str, *, chat_id: int | None = None,
    ) -> list[float] | None:
        try:
            text = text.strip()
            if not text:
                return None
            resp = await self._client.embeddings.create(
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
        except Exception as exc:
            log.warning("OpenAI embedding error: %s", exc)
            return None

    # ----------------------------------------------------------- images
    async def generate_image(
        self, prompt: str, *, size: str = "1024x1024",
        chat_id: int | None = None,
    ) -> bytes | None:
        """Return raw PNG bytes for the generated image."""
        try:
            resp = await self._client.images.generate(
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

    # ----------------------------------------------------------- audio
    async def transcribe(
        self, audio: BinaryIO, filename: str = "audio.ogg",
        chat_id: int | None = None,
    ) -> str | None:
        try:
            audio.seek(0)
            file_tuple = (filename, audio.read())
            resp = await self._client.audio.transcriptions.create(
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
        try:
            audio.seek(0)
            file_tuple = (filename, audio.read())
            resp = await self._client.audio.translations.create(
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
