"""Pull a relevant photo off the web for a quiz item.

Uses LoremFlickr — a keyless service that returns a Creative-Commons Flickr
photo matching keywords — so quiz illustrations work without on-device image
generation. Best effort: any failure returns None and the quiz falls back to
its emoji text flow. The bytes are cached per item, so each keyword is fetched
once and reused everywhere.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

_BASE = "https://loremflickr.com"
_USER_AGENT = "iPedroQuizBot/1.0 (Telegram personality-quiz illustrations)"
_MAX_BYTES = 5_000_000
_WIDTH, _HEIGHT = 640, 480


async def fetch(query: str, *, timeout: float = 20.0) -> bytes | None:
    """Return JPEG bytes of a web photo matching `query`, or None on failure.

    Multi-word queries become comma tags (match any), which reliably returns
    something on-theme rather than nothing.
    """
    if not query or not query.strip():
        return None
    tags = ",".join(query.split())
    url = f"{_BASE}/{_WIDTH}/{_HEIGHT}/{quote(tags, safe=',')}"
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": _USER_AGENT}, timeout=timeout,
            follow_redirects=True,
        ) as client:
            return await _download(client, url)
    except Exception as exc:
        log.warning("web image fetch failed for %r: %s", query, exc)
        return None


async def _download(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            if not resp.headers.get("content-type", "").startswith("image/"):
                return None
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _MAX_BYTES:
                    return None
                chunks.append(chunk)
            return b"".join(chunks) or None
    except Exception as exc:
        log.info("web image download failed (%s): %s", url, exc)
        return None
