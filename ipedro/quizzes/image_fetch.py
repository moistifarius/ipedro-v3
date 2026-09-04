"""Pull a relevant photo off the web for a quiz item.

Bing image search is the primary source (most-relevant result for a keyword),
with LoremFlickr as a keyless fallback so there's always something. Best
effort: any failure returns None and the quiz falls back to its emoji text
flow. Results are cached per item, so each keyword is fetched once and reused.

Only JPEG/PNG are returned, since those are what Telegram's sendPhoto accepts
reliably (webp/gif candidates are skipped).
"""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_BING = "https://www.bing.com/images/search"
_LOREM = "https://loremflickr.com/640/480"
_MURL = re.compile(r"&quot;murl&quot;:&quot;(.*?)&quot;")
_OK_MIME = {"image/jpeg", "image/png"}
_MAX_BYTES = 8_000_000
_CANDIDATES = 10


async def fetch(query: str, *, timeout: float = 20.0) -> bytes | None:
    """Return JPEG/PNG bytes of a web photo matching `query`, or None."""
    if not query or not query.strip():
        return None
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=timeout, follow_redirects=True,
        ) as client:
            img = await _bing(client, query.strip())
            if img:
                return img
            return await _lorem(client, query.strip())
    except Exception as exc:
        log.warning("web image fetch failed for %r: %s", query, exc)
        return None


async def _bing(client: httpx.AsyncClient, query: str) -> bytes | None:
    try:
        resp = await client.get(_BING, params={"q": query, "form": "HDRSC2"})
        resp.raise_for_status()
        urls = [html.unescape(u) for u in _MURL.findall(resp.text)]
        for url in urls[:_CANDIDATES]:
            data = await _download(client, url)
            if data:
                return data
    except Exception as exc:
        log.info("bing image search failed for %r: %s", query, exc)
    return None


async def _lorem(client: httpx.AsyncClient, query: str) -> bytes | None:
    tags = ",".join(query.split())
    return await _download(client, f"{_LOREM}/{quote(tags, safe=',')}")


async def _download(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if mime not in _OK_MIME:
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
