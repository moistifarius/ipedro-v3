"""Non-Reddit meme sources: Giphy, Imgur, and KnowYourMeme.

Each source degrades gracefully — missing API key, network failure, or a
blocked request just yields no candidates and the hunt continues with the
other sources.

* Giphy   — real search API, free key (developers.giphy.com). Animated
            memes/reaction gifs. Optional: GIPHY_API_KEY.
* Imgur   — gallery search API, free registered client id
            (api.imgur.com). Huge meme corpus, and gallery posts have
            comments we can use as the caption, same as Reddit's top
            comment. Optional: IMGUR_CLIENT_ID.
* KYM     — no API; we do ONE light HTML fetch of the public search page
            per meme ask, purely to learn the NAMES of meme formats
            related to the topic ("distracted boyfriend", …). Those names
            then feed the other searches, which is where KYM's taxonomy
            shines. Best-effort: any failure (Cloudflare, layout change)
            silently returns nothing.

Candidates are normalized to a common dict shape the judge already
understands: {source, title, subreddit?, link_flair_text?, media: Media,
imgur_id?}. Pure parsers are split from fetchers for unit tests.
"""

from __future__ import annotations

import logging
import re

import httpx

from ipedro.reddit import Media

log = logging.getLogger(__name__)

_TIMEOUT = 10.0
_KYM_TIMEOUT = 6.0


# ────────────────────────────── Giphy ──────────────────────────────────────
def parse_giphy_results(payload: dict, limit: int = 6) -> list[dict]:
    """Giphy /v1/gifs/search JSON → candidate dicts (kind=animation)."""
    out: list[dict] = []
    for item in (payload.get("data") or [])[: limit * 2]:
        images = item.get("images") or {}
        original = images.get("original") or {}
        url = original.get("mp4") or original.get("url")
        if not url:
            continue
        title = (item.get("title") or "").strip() or "(untitled gif)"
        out.append({
            "source": "giphy",
            "title": title,
            "media": Media("animation", url),
        })
        if len(out) >= limit:
            break
    return out


async def giphy_candidates(
    query: str, api_key: str, *, limit: int = 6, user_agent: str = "",
) -> list[dict]:
    if not (query or "").strip() or not api_key:
        return []
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            headers={"User-Agent": user_agent or "ipedro/1.0"},
        ) as client:
            resp = await client.get(
                "https://api.giphy.com/v1/gifs/search",
                params={
                    "api_key": api_key, "q": f"{query} meme",
                    "limit": 15, "rating": "pg-13", "lang": "en",
                },
            )
            resp.raise_for_status()
            return parse_giphy_results(resp.json(), limit)
    except Exception as exc:
        log.info("giphy search failed (%s): %s", query, exc)
        return []


# ────────────────────────────── Imgur ──────────────────────────────────────
_IMGUR_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def parse_imgur_results(payload: dict, limit: int = 6) -> list[dict]:
    """Imgur /3/gallery/search JSON → candidate dicts. Albums use their
    first image; NSFW posts are dropped; animated items become videos
    (mp4) or animations (gif)."""
    out: list[dict] = []
    for item in (payload.get("data") or []):
        if item.get("nsfw"):
            continue
        image = item
        if item.get("is_album"):
            images = item.get("images") or []
            if not images:
                continue
            image = images[0]
            if image.get("nsfw"):
                continue
        link = image.get("link") or ""
        mp4 = image.get("mp4")
        if image.get("animated") and mp4:
            media = Media("video", mp4)
        elif link.lower().endswith(".gif"):
            media = Media("animation", link)
        elif link.lower().endswith(_IMGUR_IMAGE_EXTS):
            media = Media("photo", link)
        else:
            continue
        title = (item.get("title") or "").strip() or "(untitled)"
        out.append({
            "source": "imgur",
            "title": title,
            "media": media,
            "imgur_id": item.get("id") or "",
        })
        if len(out) >= limit:
            break
    return out


async def imgur_candidates(
    query: str, client_id: str, *, limit: int = 6, user_agent: str = "",
) -> list[dict]:
    if not (query or "").strip() or not client_id:
        return []
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            headers={
                "Authorization": f"Client-ID {client_id}",
                "User-Agent": user_agent or "ipedro/1.0",
            },
        ) as client:
            resp = await client.get(
                "https://api.imgur.com/3/gallery/search/top/all",
                params={"q": f"{query} meme"},
            )
            resp.raise_for_status()
            return parse_imgur_results(resp.json(), limit)
    except Exception as exc:
        log.info("imgur search failed (%s): %s", query, exc)
        return []


def parse_imgur_top_comment(payload: dict, max_len: int = 600) -> str | None:
    """Best usable comment body from /3/gallery/{id}/comments/best."""
    for c in (payload.get("data") or []):
        body = (c.get("comment") or "").strip()
        if not body or body in ("[deleted]", "[removed]"):
            continue
        if len(body) > max_len:
            continue
        return body
    return None


async def imgur_top_comment(
    gallery_id: str, client_id: str, *, user_agent: str = "",
) -> str | None:
    if not gallery_id or not client_id:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            headers={
                "Authorization": f"Client-ID {client_id}",
                "User-Agent": user_agent or "ipedro/1.0",
            },
        ) as client:
            resp = await client.get(
                f"https://api.imgur.com/3/gallery/{gallery_id}/comments/best",
            )
            resp.raise_for_status()
            return parse_imgur_top_comment(resp.json())
    except Exception as exc:
        log.info("imgur comments failed (%s): %s", gallery_id, exc)
        return None


# ─────────────────────────── KnowYourMeme ──────────────────────────────────
# Entry links on the search page look like <a href="/memes/distracted-
# boyfriend">…. We only want the SLUGS (format names), not images.
_KYM_ENTRY_RE = re.compile(r'href="/memes/([a-z0-9-]+)"')
_KYM_SKIP_SLUGS = frozenset({
    "confirmed", "submissions", "researching", "popular", "all",
    "trending", "deadpool",
})


def parse_kym_meme_names(html: str, limit: int = 2) -> list[str]:
    """Meme format names from a KYM search page, as plain phrases
    ('distracted-boyfriend' → 'distracted boyfriend'). Order preserved,
    deduped, section/nav slugs skipped."""
    out: list[str] = []
    for slug in _KYM_ENTRY_RE.findall(html or ""):
        if slug in _KYM_SKIP_SLUGS or slug in out:
            continue
        out.append(slug)
        if len(out) >= limit:
            break
    return [s.replace("-", " ") for s in out]


async def kym_meme_names(
    query: str, *, limit: int = 2, user_agent: str = "",
) -> list[str]:
    """Best-effort meme-format discovery. One light GET of the public
    search page; ANY failure (block, layout change, timeout) → []."""
    if not (query or "").strip():
        return []
    try:
        async with httpx.AsyncClient(
            timeout=_KYM_TIMEOUT, follow_redirects=True,
            headers={"User-Agent": user_agent or "ipedro/1.0"},
        ) as client:
            resp = await client.get(
                "https://knowyourmeme.com/search",
                params={"q": query},
            )
            resp.raise_for_status()
            return parse_kym_meme_names(resp.text, limit)
    except Exception as exc:
        log.info("kym lookup failed (%s): %s", query, exc)
        return []
