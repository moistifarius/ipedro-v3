"""Downloads the meme images/GIFs that automod responses point at.

Split out of handlers/automod.py so that file can be pure data — the
trigger table and its canned lines — which ipedro/merge_policy.py lets an
/evolve change merge without the owner looking. Code that reaches the
network doesn't belong in anything that can merge unreviewed, and neither
does the rule deciding which hosts are acceptable: that used to be checked
only by a test, and test files can merge unreviewed too, so a change could
add a URL and loosen the check in the same breath. Now it's enforced here,
at fetch time.
"""

from __future__ import annotations

import logging

import httpx

from ipedro.handlers.automod import MediaResponse

log = logging.getLogger(__name__)

# Pinned, stable media hosts (imgflip templates, KYM entry icons,
# giphy/tenor media). A URL anywhere else is never fetched.
MEDIA_HOSTS = frozenset({
    "i.imgflip.com", "i.kym-cdn.com", "media.giphy.com",
    "media.tenor.com", "media1.tenor.com",
})

# In-process cache of fetched media bytes. The URL set is small and fixed
# (~16 templates, ~10MB total worst case), so a plain dict is plenty.
_MEDIA_CACHE: dict[str, bytes] = {}
_MEDIA_TIMEOUT = 10.0
_MEDIA_MAX_BYTES = 10_000_000
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def host_allowed(url: str) -> bool:
    parts = url.split("/")
    return url.startswith("https://") and len(parts) > 2 and parts[2] in MEDIA_HOSTS


async def fetch_automod_media(media: MediaResponse) -> bytes | None:
    """Download (and cache) the bytes for a MediaResponse. None on any
    failure — including a URL on a host outside MEDIA_HOSTS, which the
    caller treats like any dead link: the text fallback fires instead."""
    if not host_allowed(media.url):
        log.warning("automod media refused, host not allowed: %s", media.url)
        return None
    cached = _MEDIA_CACHE.get(media.url)
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=_MEDIA_TIMEOUT,
            headers={"User-Agent": _UA},
        ) as client:
            async with client.stream("GET", media.url) as resp:
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MEDIA_MAX_BYTES:
                        log.info("automod media too large: %s", media.url)
                        return None
                    chunks.append(chunk)
        data = b"".join(chunks)
        if data:
            _MEDIA_CACHE[media.url] = data
            return data
    except Exception as exc:
        log.info("automod media fetch failed %s: %s", media.url, exc)
    return None
