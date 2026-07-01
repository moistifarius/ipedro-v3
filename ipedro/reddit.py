"""Pull a meme (image / gif / video) from a rotation of subreddits and the
post's top comment, so the bot can drop it into a chat.

Reddit exposes public JSON at ``/r/<sub>/top.json`` and
``<permalink>.json`` (no auth needed, but a distinct User-Agent is
required or Reddit 429s). Parsing is split into pure functions
(resolve_media / choose_post / pick_top_comment / reddit_audio_candidates)
that unit-test against fixture JSON; the async ``fetch_meme`` and
``download_media`` wrap them with HTTP + ffmpeg.

Reddit-hosted video (v.redd.it) is DASH — video and audio live in
separate files — so we download both and mux them with ffmpeg, falling
back to video-only when audio is missing or ffmpeg isn't installed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import tempfile
from dataclasses import dataclass, field

import httpx

from ipedro.radio_fx import ffmpeg_available

log = logging.getLogger(__name__)

# The rotation. Mostly-SFW meme subs; NSFW-tagged posts are filtered out
# regardless.
SUBREDDITS: tuple[str, ...] = (
    "me_irl",
    "memes",
    "coaxedintoasnafu",
    "notinteresting",
    "funny",
)

_USER_AGENT = "iPedro/1.0 (Telegram meme bot)"
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_MAX_COMMENT_LEN = 600
# Byte caps so a pathological post can't OOM the bot.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_VIDEO_BYTES = 48 * 1024 * 1024
_MAX_AUDIO_BYTES = 16 * 1024 * 1024


@dataclass
class Media:
    kind: str                 # 'photo' | 'animation' | 'video'
    url: str
    audio_candidates: list[str] = field(default_factory=list)  # v.redd.it audio


@dataclass
class Meme:
    subreddit: str
    title: str
    post_author: str
    permalink: str
    media: Media
    comment: str | None = None
    comment_author: str | None = None


# ───────────────────────────── pure helpers ───────────────────────────────
def reddit_audio_candidates(fallback_url: str) -> list[str]:
    """Candidate DASH audio URLs for a v.redd.it video, newest naming first.
    Reddit has used several audio filenames over the years; we try them in
    order until one downloads."""
    base = fallback_url.split("?", 1)[0].rsplit("/", 1)[0]
    return [
        f"{base}/DASH_AUDIO_128.mp4",
        f"{base}/DASH_AUDIO_64.mp4",
        f"{base}/DASH_audio.mp4",
    ]


def _first_gallery_media(post: dict) -> Media | None:
    """First item of a Reddit gallery post, as photo or animation."""
    gallery = post.get("gallery_data") or {}
    items = gallery.get("items") or []
    meta = post.get("media_metadata") or {}
    for item in items:
        mid = item.get("media_id")
        m = meta.get(mid) if mid else None
        if not m or m.get("status") != "valid":
            continue
        src = (m.get("s") or {})
        url = src.get("u") or src.get("gif")
        if not url:
            continue
        mime = m.get("m") or ""
        kind = "animation" if "gif" in mime else "photo"
        return Media(kind=kind, url=url)
    return None


def _preview_image(post: dict) -> str | None:
    images = (post.get("preview") or {}).get("images") or []
    if images:
        src = (images[0].get("source") or {})
        return src.get("url")
    return None


def resolve_media(post: dict) -> Media | None:
    """Best displayable media for a post, or None if nothing we can send.

    Order: Reddit video → gallery → direct image/gif/mp4 by extension →
    imgur .gifv → preview image for 'image' hints.
    """
    if post.get("over_18"):
        return None

    # Reddit-hosted video.
    if post.get("is_video"):
        rv = ((post.get("media") or {}).get("reddit_video")) or {}
        fb = rv.get("fallback_url")
        if fb:
            fb = fb.split("?", 1)[0]
            audio = (
                reddit_audio_candidates(fb)
                if rv.get("has_audio", True) else []
            )
            return Media(kind="video", url=fb, audio_candidates=audio)

    # Gallery.
    if post.get("is_gallery"):
        gm = _first_gallery_media(post)
        if gm:
            return gm

    url = post.get("url_overridden_by_dest") or post.get("url") or ""
    low = url.lower().split("?", 1)[0]
    if low.endswith(_IMAGE_EXTS):
        return Media(kind="photo", url=url)
    if low.endswith(".gif"):
        return Media(kind="animation", url=url)
    if low.endswith(".gifv"):        # imgur animated → direct mp4
        return Media(kind="video", url=url[:-5] + ".mp4")
    if low.endswith(".mp4"):
        return Media(kind="video", url=url)

    # Last resort: a preview image Reddit generated for an 'image' link.
    if post.get("post_hint") == "image":
        prev = _preview_image(post)
        if prev:
            return Media(kind="photo", url=prev)
    return None


def choose_post(
    listing: dict, rng: random.Random | None = None,
) -> dict | None:
    """Pick a random displayable, SFW, non-stickied post from a listing."""
    r = rng or random
    children = (listing.get("data") or {}).get("children") or []
    candidates = []
    for c in children:
        if c.get("kind") != "t3":
            continue
        d = c.get("data") or {}
        if d.get("over_18") or d.get("stickied") or d.get("is_self"):
            continue
        if resolve_media(d) is None:
            continue
        candidates.append(d)
    if not candidates:
        return None
    return r.choice(candidates)


def pick_top_comment(
    comments_listing: dict, max_len: int = _MAX_COMMENT_LEN,
) -> tuple[str | None, str | None]:
    """Highest-voted usable comment (body, author) from a permalink's
    comments listing (already sorted top). Skips removed/deleted, the
    AutoModerator, stickied, and over-long comments."""
    children = (comments_listing.get("data") or {}).get("children") or []
    for c in children:
        if c.get("kind") != "t1":
            continue
        d = c.get("data") or {}
        if d.get("stickied"):
            continue
        body = (d.get("body") or "").strip()
        author = d.get("author") or ""
        if not body or body in ("[removed]", "[deleted]"):
            continue
        if author.lower() in ("automoderator", "[deleted]"):
            continue
        if len(body) > max_len:
            continue
        return body, author
    return None, None


# ───────────────────────────── async fetch ────────────────────────────────
async def _get_json(client: httpx.AsyncClient, url: str, **params):
    try:
        resp = await client.get(url, params=params or None)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # network / decode / status
        log.info("reddit fetch failed (%s): %s", url, exc)
        return None


async def fetch_meme(
    rng: random.Random | None = None,
    subreddits: tuple[str, ...] = SUBREDDITS,
    *,
    timeout: float = 12.0,
    user_agent: str | None = None,
) -> Meme | None:
    """Fetch one meme with a top comment. Tries subreddits in random order
    until one yields displayable media; returns None if all fail."""
    r = rng or random
    subs = list(subreddits)
    r.shuffle(subs)
    # Vary the listing window so it's not the same handful every time.
    window = r.choice(("day", "week"))
    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent or _USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    ) as client:
        for sub in subs:
            listing = await _get_json(
                client, f"https://www.reddit.com/r/{sub}/top.json",
                t=window, limit=75, raw_json=1,
            )
            if not listing:
                continue
            post = choose_post(listing, r)
            if not post:
                continue
            media = resolve_media(post)
            if media is None:
                continue
            comment = author = None
            comments = await _get_json(
                client,
                f"https://www.reddit.com{post.get('permalink', '')}.json",
                sort="top", limit=25, raw_json=1,
            )
            if isinstance(comments, list) and len(comments) >= 2:
                comment, author = pick_top_comment(comments[1])
            return Meme(
                subreddit=sub,
                title=post.get("title") or "",
                post_author=post.get("author") or "",
                permalink=post.get("permalink") or "",
                media=media,
                comment=comment,
                comment_author=author,
            )
    return None


# ───────────────────────────── media download ─────────────────────────────
async def _download(
    client: httpx.AsyncClient, url: str, cap: int,
) -> bytes | None:
    try:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > cap:
                    log.info("reddit media over cap (%s): %s bytes", url, total)
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except Exception as exc:
        log.info("reddit download failed (%s): %s", url, exc)
        return None


async def _mux(video: bytes, audio: bytes) -> bytes | None:
    """ffmpeg -c copy mux of separate video+audio into one MP4."""
    if not ffmpeg_available():
        return None
    vpath = apath = opath = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as vf:
            vf.write(video)
            vpath = vf.name
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as af:
            af.write(audio)
            apath = af.name
        opath = vpath + ".out.mp4"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", vpath, "-i", apath, "-c", "copy", "-shortest", opath,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return None
        if proc.returncode != 0 or not os.path.exists(opath):
            return None
        with open(opath, "rb") as fh:
            return fh.read()
    except Exception as exc:  # pragma: no cover - defensive
        log.info("reddit video mux failed: %s", exc)
        return None
    finally:
        for p in (vpath, apath, opath):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


async def download_media(
    media: Media, *, timeout: float = 20.0, user_agent: str | None = None,
) -> bytes | None:
    """Download the media bytes, muxing audio into Reddit video when
    possible. Returns None on failure."""
    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent or _USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    ) as client:
        if media.kind == "video":
            video = await _download(client, media.url, _MAX_VIDEO_BYTES)
            if not video:
                return None
            for au in media.audio_candidates:
                audio = await _download(client, au, _MAX_AUDIO_BYTES)
                if audio:
                    muxed = await _mux(video, audio)
                    if muxed:
                        return muxed
                    break  # audio existed but mux failed → send video-only
            return video  # no audio track / mux unavailable → video-only
        cap = _MAX_IMAGE_BYTES
        return await _download(client, media.url, cap)


def build_caption(meme: Meme, *, limit: int = 1024) -> str:
    """The chat caption: the top comment (the bot 'saying' it), with a
    small provenance footer. Falls back to the post title when there's no
    usable comment."""
    footer = f"\n\n· r/{meme.subreddit}"
    body = (meme.comment or meme.title or "").strip()
    budget = limit - len(footer)
    if len(body) > budget:
        body = body[: budget - 1].rstrip() + "…"
    return f"{body}{footer}"
