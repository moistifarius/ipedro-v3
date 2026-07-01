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
import re
import tempfile
import time
from dataclasses import dataclass, field

import httpx

from ipedro.radio_fx import ffmpeg_available

log = logging.getLogger(__name__)

# OAuth token host vs data host. Reddit blocks anonymous www.reddit.com/.json
# from datacenter IPs (403); with a "script" app's client_id/secret we mint
# an application-only (read-only) bearer token at the token host and make
# all data calls to the OAuth host. See ipedro/reddit.py header + docs.
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_OAUTH_BASE = "https://oauth.reddit.com"
_ANON_BASE = "https://www.reddit.com"
# Refresh a bit before the token actually expires.
_TOKEN_REFRESH_MARGIN = 120.0
_TOKEN_FALLBACK_TTL = 3600.0  # used only if the response omits expires_in

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
    # Some top comments ARE media (a Giphy gif, an image reply). When so,
    # this holds the gif/image to post instead of the raw markdown.
    comment_media: Media | None = None
    # Cleaned text of the top few comments — raw material for the bot to
    # write its OWN reaction from (see build_reddit_comment_prompt).
    top_comments: list[str] = field(default_factory=list)


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


# Reddit comment media embeds. A Giphy gif comment's body looks like
# '![gif](giphy|641arBi22PAty)'; an image reply is '![img](abc123)'. The
# referenced id keys into the comment's media_metadata.
_GIPHY_RE = re.compile(r"!\[gif\]\(giphy\|([A-Za-z0-9]+)", re.IGNORECASE)
_EMBED_RE = re.compile(r"!\[(?:gif|img)\]\([^)]*\)", re.IGNORECASE)


def clean_comment_text(body: str) -> str:
    """Strip inline media-embed markdown so we never show a raw
    '![gif](giphy|...)' token as text. Returns the remaining prose."""
    return _EMBED_RE.sub("", body or "").strip()


def extract_comment_media(comment: dict) -> Media | None:
    """The gif/image a comment embeds, or None. Prefers the authoritative
    media_metadata (handles Giphy gifs and image replies); falls back to
    building a Giphy URL from the id in the body."""
    meta = comment.get("media_metadata") or {}
    for m in meta.values():
        if not isinstance(m, dict) or m.get("status") != "valid":
            continue
        s = m.get("s") or {}
        mime = (m.get("m") or "").lower()
        etype = (m.get("e") or "").lower()
        if etype == "animatedimage" or "gif" in mime:
            url = s.get("mp4") or s.get("gif")     # mp4 is smaller; both loop
            if url:
                return Media("animation", url)
        if etype == "image" or mime.startswith("image/"):
            url = s.get("u") or s.get("gif")
            if url:
                return Media("photo", url)
    gm = _GIPHY_RE.search(comment.get("body") or "")
    if gm:
        return Media(
            "animation",
            f"https://i.giphy.com/media/{gm.group(1)}/giphy.gif",
        )
    return None


def pick_top_comments(
    comments_listing: dict, limit: int = 6, max_len: int = _MAX_COMMENT_LEN,
) -> list[dict]:
    """Up to ``limit`` usable comment DATA dicts from a permalink's comments
    listing (already sorted top → highest first). Skips removed/deleted,
    the AutoModerator, stickied, and over-long text comments (media-only
    comments like a gif embed are kept regardless of the length cap)."""
    children = (comments_listing.get("data") or {}).get("children") or []
    out: list[dict] = []
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
        if len(body) > max_len and extract_comment_media(d) is None:
            continue
        out.append(d)
        if len(out) >= limit:
            break
    return out


def pick_top_comment(
    comments_listing: dict, max_len: int = _MAX_COMMENT_LEN,
) -> dict | None:
    """The single highest-voted usable comment dict (or None). The caller
    derives display text (clean_comment_text), author, and embedded media
    (extract_comment_media) from it."""
    picked = pick_top_comments(comments_listing, limit=1, max_len=max_len)
    return picked[0] if picked else None


# ─────────────────────────── AI reaction prompt ───────────────────────────
def build_reddit_comment_prompt(
    meme: "Meme", member_names: list[str], persona_text: str,
    *, extra_context: str | None = None,
) -> list[dict]:
    """Messages array for generating the bot's OWN reaction to a meme,
    built FROM the top Reddit comments as inspiration, tailored to who's in
    the chat, in the chat's persona voice. Pure/testable."""
    comments = "\n".join(f"- {c}" for c in meme.top_comments[:6]) or "(none)"
    # Dedup + cap the member list so a big chat doesn't blow the prompt.
    seen: list[str] = []
    for n in member_names:
        n = (n or "").strip()
        if n and n not in seen:
            seen.append(n)
    members = ", ".join(seen[:15]) if seen else "(unknown)"
    system = (
        f"{persona_text}\n\n"
        "A meme just got posted in your group chat. React to it with ONE "
        "short line (under 200 characters), in your voice, like one of the "
        "crew chiming in. IMPORTANT: base your reaction on the top reddit "
        "comments below — take the angle, joke, or observation people "
        "latched onto and re-cast it in your own words for this chat. Build "
        "on their take; just don't quote them word-for-word. You MAY "
        "name-drop someone in the chat if it genuinely fits, but never "
        "force it and never @ everyone. No hashtags, no 'as an AI', keep "
        "emoji minimal. Output ONLY the reaction line."
    )
    user = (
        f"Meme: {meme.title or '(untitled)'} (from r/{meme.subreddit})\n\n"
        f"Top reddit comments — your inspiration, build on these "
        f"(don't quote verbatim):\n{comments}\n\n"
        f"People in this chat: {members}"
    )
    if extra_context:
        user += f"\n\nWhat's been going on in the chat lately:\n{extra_context}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ───────────────────────────── URL builders (pure) ────────────────────────
def _listing_url(base: str, sub: str, json_suffix: bool) -> str:
    return f"{base}/r/{sub}/top{'.json' if json_suffix else ''}"


def _comments_url(base: str, permalink: str, json_suffix: bool) -> str:
    # Anonymous host wants a trailing-slash-free path + .json; the OAuth host
    # returns JSON natively and 404s on a .json suffix.
    if json_suffix:
        return f"{base}{permalink.rstrip('/')}.json"
    return f"{base}{permalink}"


# ───────────────────────────── OAuth token cache ──────────────────────────
# After a failed mint (bad creds, 401, network), don't hammer the token
# endpoint on every /redditmeme — back off for this long, then retry.
_TOKEN_FAILURE_COOLDOWN = 60.0
_token_cache: dict = {"token": None, "expires_at": 0.0, "retry_after": 0.0}
_token_lock = asyncio.Lock()


def reset_token_cache() -> None:
    """Drop the cached bearer token and any failure back-off (tests /
    forced refresh — e.g. /debug_redditmeme wants a fresh attempt)."""
    _token_cache["token"] = None
    _token_cache["expires_at"] = 0.0
    _token_cache["retry_after"] = 0.0


async def _get_oauth_token(
    client_id: str, client_secret: str, user_agent: str,
) -> str | None:
    """Application-only (client_credentials) bearer token, cached until
    shortly before it expires. Returns None if acquisition fails, and
    briefly backs off so a bad-credential setup doesn't retry-storm the
    token endpoint (which would draw Reddit's rate limiter)."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]
    if now < _token_cache["retry_after"]:
        return None  # recent failure; don't hammer
    async with _token_lock:
        now = time.time()
        if _token_cache["token"] and now < _token_cache["expires_at"]:
            return _token_cache["token"]
        if now < _token_cache["retry_after"]:
            return None
        try:
            async with httpx.AsyncClient(
                timeout=15.0, follow_redirects=True,
            ) as client:
                resp = await client.post(
                    _TOKEN_URL,
                    auth=(client_id, client_secret),   # HTTP Basic
                    headers={"User-Agent": user_agent},
                    data={"grant_type": "client_credentials"},  # form-encoded
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            log.warning("reddit token fetch failed: %s", exc)
            _token_cache["retry_after"] = time.time() + _TOKEN_FAILURE_COOLDOWN
            return None
        token = payload.get("access_token")
        if not token:
            log.warning("reddit token response had no access_token: %s", payload)
            _token_cache["retry_after"] = time.time() + _TOKEN_FAILURE_COOLDOWN
            return None
        ttl = float(payload.get("expires_in") or _TOKEN_FALLBACK_TTL)
        _token_cache["token"] = token
        _token_cache["expires_at"] = time.time() + max(60.0, ttl - _TOKEN_REFRESH_MARGIN)
        _token_cache["retry_after"] = 0.0
        log.info("reddit oauth token acquired (ttl=%.0fs).", ttl)
        return token


async def _api_context(
    client_id: str, client_secret: str, user_agent: str,
) -> tuple[str, dict, bool]:
    """Resolve (base_url, request_headers, needs_json_suffix).

    With credentials we authenticate and use the OAuth host; otherwise we
    fall back to the anonymous host (which usually 403s from servers, but
    is better than nothing on a residential IP)."""
    headers = {"User-Agent": user_agent}
    if client_id and client_secret:
        token = await _get_oauth_token(client_id, client_secret, user_agent)
        if token:
            headers["Authorization"] = f"bearer {token}"
            return _OAUTH_BASE, headers, False
    return _ANON_BASE, headers, True


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
    client_id: str = "",
    client_secret: str = "",
) -> Meme | None:
    """Fetch one meme with a top comment. Tries subreddits in random order
    until one yields displayable media; returns None if all fail."""
    r = rng or random
    ua = user_agent or _USER_AGENT
    subs = list(subreddits)
    r.shuffle(subs)
    # Vary the listing window so it's not the same handful every time.
    window = r.choice(("day", "week"))
    base, headers, json_suffix = await _api_context(client_id, client_secret, ua)
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, follow_redirects=True,
    ) as client:
        for sub in subs:
            listing = await _get_json(
                client, _listing_url(base, sub, json_suffix),
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
            comment_media = None
            top_comments: list[str] = []
            comments = await _get_json(
                client,
                _comments_url(base, post.get("permalink", ""), json_suffix),
                sort="top", limit=25, raw_json=1,
            )
            if isinstance(comments, list) and len(comments) >= 2:
                cdicts = pick_top_comments(comments[1], limit=6)
                for cd in cdicts:
                    t = clean_comment_text(cd.get("body") or "")
                    if t:
                        top_comments.append(t)
                if cdicts:
                    first = cdicts[0]
                    author = first.get("author") or None
                    comment_media = extract_comment_media(first)
                    comment = clean_comment_text(first.get("body") or "") or None
            return Meme(
                subreddit=sub,
                title=post.get("title") or "",
                post_author=post.get("author") or "",
                permalink=post.get("permalink") or "",
                media=media,
                comment=comment,
                comment_author=author,
                comment_media=comment_media,
                top_comments=top_comments,
            )
    return None


async def diagnose(
    *, user_agent: str = "", client_id: str = "", client_secret: str = "",
    timeout: float = 15.0,
) -> dict:
    """Structured self-diagnosis for /debug_redditmeme: which mode, whether
    the token was obtained, and the HTTP status of a real listing call."""
    ua = user_agent or _USER_AGENT
    report: dict = {
        "credentials_set": bool(client_id and client_secret),
        "user_agent": ua,
        "mode": "anonymous",
        "token_ok": None,
        "listing_status": None,
        "listing_children": None,
        "error": None,
    }
    reset_token_cache()
    try:
        base, headers, json_suffix = await _api_context(client_id, client_secret, ua)
        report["mode"] = "oauth" if base == _OAUTH_BASE else "anonymous"
        if report["credentials_set"]:
            report["token_ok"] = "Authorization" in headers
        async with httpx.AsyncClient(
            headers=headers, timeout=timeout, follow_redirects=True,
        ) as client:
            resp = await client.get(
                _listing_url(base, SUBREDDITS[0], json_suffix),
                params={"t": "day", "limit": 5, "raw_json": 1},
            )
            report["listing_status"] = resp.status_code
            if resp.status_code == 200:
                try:
                    j = resp.json()
                    report["listing_children"] = len(
                        (j.get("data") or {}).get("children") or []
                    )
                except Exception:
                    report["error"] = "200 but body was not JSON"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report


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
