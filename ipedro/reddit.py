"""Pull a post (image / gif / video) from Reddit's r/popular feed and the
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

# We pull from r/popular — Reddit's cross-community trending feed (already
# SFW-filtered by Reddit; NSFW-tagged posts are dropped again regardless).
# The post's real source community is surfaced in the caption footer.
SUBREDDITS: tuple[str, ...] = ("popular",)

# Topic searches ("meme about X") look in the big meme communities first —
# relevance search inside them returns actual memes rather than news posts.
# Sitewide search (with 'meme' appended to the query) is the fallback.
MEME_SEARCH_SUBS: tuple[str, ...] = (
    "memes", "me_irl", "funny", "dankmemes", "wholesomememes",
)

# Communities where a media post IS (nearly always) a meme, beyond the
# search rotation above. Used by is_meme_flavored.
_MEME_SUB_SET: frozenset[str] = frozenset(
    s.lower() for s in MEME_SEARCH_SUBS
) | frozenset({
    "meirl", "2meirl4meirl", "adviceanimals", "shitposting",
    "comedyheaven", "okbuddyretard", "memesopdidnotlike", "dankvideos",
})

# Post flair that marks a meme/joke post inside a non-meme community —
# most topic subs flair them ("Meme", "Shitpost", "Humor", …).
_MEME_FLAIR_RE = re.compile(
    r"meme|shit\s*post|humou?r|funny|joke|comic|satire", re.IGNORECASE,
)


def is_meme_flavored(post: dict) -> bool:
    """Heuristic: does this post LOOK like a meme (vs a news photo, game
    highlight, or ordinary picture)? True when it lives in a known meme
    community, carries a meme-ish flair, or says 'meme' in its title.
    Used to filter topic-sub pulls and to sink non-meme candidates below
    meme ones before the judge sees them."""
    sub = (post.get("subreddit") or "").lower()
    if sub in _MEME_SUB_SET:
        return True
    flair = post.get("link_flair_text") or ""
    if flair and _MEME_FLAIR_RE.search(flair):
        return True
    title = post.get("title") or ""
    return bool(re.search(r"\bmemes?\b", title, re.IGNORECASE))

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
    # Where the meme came from: 'reddit' | 'giphy' | 'imgur'. Controls the
    # caption footer ('· r/<sub>' vs '· giphy').
    source: str = "reddit"


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


def _displayable_posts(listing: dict, top_k: int | None = None) -> list[dict]:
    """Displayable, SFW, non-stickied post dicts from a listing, in listing
    order (which is relevance/score order for searches). ``top_k`` caps the
    result."""
    children = (listing.get("data") or {}).get("children") or []
    out: list[dict] = []
    for c in children:
        if c.get("kind") != "t3":
            continue
        d = c.get("data") or {}
        if d.get("over_18") or d.get("stickied") or d.get("is_self"):
            continue
        if resolve_media(d) is None:
            continue
        out.append(d)
        if top_k is not None and len(out) >= top_k:
            break
    return out


def choose_post(
    listing: dict, rng: random.Random | None = None,
    *, top_k: int | None = None,
) -> dict | None:
    """Pick a random displayable post from a listing (see
    ``_displayable_posts``). ``top_k`` limits the pool to the first K
    candidates — used for relevance-sorted search results, where the
    further down the list you go the less the post has to do with the
    query."""
    r = rng or random
    candidates = _displayable_posts(listing, top_k)
    if not candidates:
        return None
    return r.choice(candidates)


def pick_topic_subreddits(
    listing: dict, limit: int = 2, min_subscribers: int = 5000,
) -> list[str]:
    """Community names from a /subreddits/search listing worth pulling
    topic memes from: public, SFW, not a user profile, and big enough to
    actually have content. Preserves Reddit's relevance order."""
    children = (listing.get("data") or {}).get("children") or []
    out: list[str] = []
    for c in children:
        if c.get("kind") != "t5":
            continue
        d = c.get("data") or {}
        name = d.get("display_name") or ""
        if not name or name.lower().startswith("u_"):
            continue
        if d.get("over18") or d.get("over_18"):
            continue
        if (d.get("subreddit_type") or "public") != "public":
            continue
        if int(d.get("subscribers") or 0) < min_subscribers:
            continue
        out.append(name)
        if len(out) >= limit:
            break
    return out


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


def pick_top_comment(
    comments_listing: dict, max_len: int = _MAX_COMMENT_LEN,
) -> dict | None:
    """Highest-voted usable comment DATA dict from a permalink's comments
    listing (already sorted top). Skips removed/deleted, the AutoModerator,
    stickied, and over-long comments. The caller derives the display text
    (clean_comment_text), author, and any embedded media
    (extract_comment_media) from the returned dict."""
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
        # A media-only comment (just a gif embed) has a short body and is
        # fine; the length cap only guards against walls of text.
        if len(body) > max_len and extract_comment_media(d) is None:
            continue
        return d
    return None


# ─────────────────────── meme-request detection (pure) ────────────────────
# "hey pedro give me a meme about the game" / "can you find a meme about
# this" / "any memes about mondays?" / "meme this". Detection must not
# hijack casual mentions ("that meme about cats was funny", "I'll send a
# meme about it later", "did you see the meme this morning?"), so:
#   * the request VERB must sit in imperative position — the start of the
#     message/clause, or right after the bot's name / "please" / a
#     "can|could|will|would you" lead-in;
#   * the question form requires an any/you-subject shape ("any memes
#     about", "do you have a meme about", "you got a meme about") — bare
#     got/have would match declaratives and negations;
#   * "meme this" must be (basically) the whole message.
# These also only run on messages the bot was answering anyway.
# Every name the bot answers to (current persona + legacy aliases) —
# "duder gimme a meme about that" is as much a request as "pedro …".
_BOT_NAMES = (
    r"(?:pedro|idale|dale(?:\s+gribble)?|rusty(?:\s+shackleford)?"
    r"|boomhauer|dude(?:r(?:ino)?)?|el\s+duderino|bot)"
)
_REQ_PREFIX = (
    r"(?:^|[.!?]\s+|,\s*"
    r"|\b" + _BOT_NAMES + r"\b[,!:]?\s+"
    r"|\b(?:please|pls)\s+"
    r"|\b(?:can|could|will|would)\s+(?:you|u|i|we)\s+(?:please\s+)?"
    r"|\blemme\s+"
    r"|\bsome(?:one|body)\s+)"
)
_MEME_VERB_RE = re.compile(
    _REQ_PREFIX +
    r"(?:gimme|give\s+(?:me|us)|find(?:\s+(?:me|us))?|post|drop|send"
    r"(?:\s+(?:me|us))?|show\s+(?:me|us)|get(?:\s+(?:me|us))?"
    r"|pull(?:\s+up)?|make(?:\s+(?:me|us))?|create|generate|design"
    r"|whip\s+up|cook\s+up"
    r"|throw\s+(?:me|us)|hit\s+(?:me|us)\s+with)\s+"
    r"(?:a\s+|some\s+|another\s+|me\s+a\s+|us\s+a\s+)?memes?\b"
    r"(?:\s+(?:about|of|on|for|regarding)\s+(?P<topic>.+))?",
    re.IGNORECASE,
)
# First-person desire — "i want a meme about X" / "we need some memes of
# Y". Request semantics without imperative form.
_MEME_WANT_RE = re.compile(
    r"\b(?:i|we)\s+(?:want|need|could\s+use|demand)\s+"
    r"(?:a\s+|some\s+|another\s+)?memes?\b"
    r"(?:\s+(?:about|of|on|for)\s+(?P<topic>.+))?",
    re.IGNORECASE,
)
# Bare noun-first ask, gated on imperative position so casual mentions
# ("that meme about cats was funny") stay dead: "pedro meme about cats" /
# "meme of shrek please".
_MEME_BARE_RE = re.compile(
    _REQ_PREFIX +
    r"(?:a\s+|some\s+)?memes?\s+(?:about|of)\s+(?P<topic>.+)",
    re.IGNORECASE,
)
# "make this a meme" / "turn that into a meme" — deictic transforms.
_MEME_TRANSFORM_RE = re.compile(
    _REQ_PREFIX +
    r"(?:make|turn)\s+(?:this|that|it)\s+(?:into\s+)?a\s+meme\b",
    re.IGNORECASE,
)
_MEME_QUESTION_RE = re.compile(
    r"(?:\b(?:any|got\s+any|have\s+any"
    r"|do\s+(?:you|u)\s+(?:have|got)(?:\s+(?:a|any|some))?"
    r"|(?:you|u)\s+got(?:\s+(?:a|any|some))?)"
    r"|" + _REQ_PREFIX + r"got(?:\s+(?:a|any|some))?)\s+"
    r"memes?\s+(?:about|of|for)\s+(?P<topic>.+)",
    re.IGNORECASE,
)
# Whole-message imperative only (leading hey/bot-name and trailing
# punctuation allowed) — "meme this" mid-sentence is never a request.
_MEME_THIS_RE = re.compile(
    r"^\W*(?:(?:hey|ok|okay|yo)[,!\s]+)?"
    r"(?:" + _BOT_NAMES + r"[,!:\s]+)?"
    r"memes?\s+(?:this|that)[\s.!?]*$",
    re.IGNORECASE,
)

# Generation verbs — "make/create/generate/whip up a meme" means MAKE one
# from scratch (image model), not fetch an existing one. Detected on the
# raw text independently of which pattern matched the request, so it works
# whether the fast grammar or the AI classifier caught it.
_MEME_MAKE_VERB_RE = re.compile(
    _REQ_PREFIX +
    r"(?:make|create|generate|design|whip\s+up|cook\s+up)\s+"
    r"(?:me\s+|us\s+|a\s+|an\s+|another\s+|some\s+)*memes?\b",
    re.IGNORECASE,
)


def is_meme_generation_request(text: str | None) -> bool:
    """True when a meme request used a GENERATE verb (make / create /
    generate / whip up, or 'make this a meme') rather than a find/fetch
    verb. Only meaningful alongside a non-None detect_meme_request()."""
    if not text:
        return False
    return bool(
        _MEME_MAKE_VERB_RE.search(text) or _MEME_TRANSFORM_RE.search(text)
    )


# Topics that mean "the current conversation" rather than a literal subject.
_DEICTIC_TOPICS = frozenset({
    "this", "that", "it", "us", "this convo", "the convo",
    "this conversation", "the conversation", "this chat", "the chat",
    "the current topic", "what we're talking about",
    "what were talking about", "whatever",
})
# Politeness / time-adverb tails are safe to strip repeatedly; vocatives
# (man/dude/pedro/dale) ONLY when comma-separated, so real topics like
# "the dude" or "pedro" survive.
_TOPIC_FILLER_RE = re.compile(
    r"\s+(?:please|pls|plz|thanks|thx|now|rn|asap|lol|lmao"
    r"|real\s+quick|later|tomorrow|today|tonight|soon|again)$"
    r"|,\s*(?:man|dude|dale|pedro)$",
    re.IGNORECASE,
)


def detect_meme_request(text: str | None) -> str | None:
    """Detect a meme request in a chat message.

    Returns None when there's no request; '' when the topic should be
    derived from the current conversation ("meme about this", or no topic
    given); otherwise the explicit topic string to search for.
    """
    if not text:
        return None
    if _MEME_THIS_RE.match(text):
        return ""
    if _MEME_TRANSFORM_RE.search(text):
        return ""
    m = (
        _MEME_VERB_RE.search(text)
        or _MEME_QUESTION_RE.search(text)
        or _MEME_WANT_RE.search(text)
        or _MEME_BARE_RE.search(text)
    )
    if not m:
        return None
    topic = (m.group("topic") or "").strip()
    # Trim at clause boundaries (commas included — "about cats, and also…"),
    # then peel politeness/time filler.
    topic = re.split(r"[,?!.;:]", topic, maxsplit=1)[0].strip()
    prev = None
    while topic and topic != prev:
        prev = topic
        topic = _TOPIC_FILLER_RE.sub("", topic).strip()
    topic = topic.strip(" ,\"'")
    if not topic or topic.lower() in _DEICTIC_TOPICS:
        return ""
    return topic


# ───────────────────────────── URL builders (pure) ────────────────────────
def _listing_url(base: str, sub: str, json_suffix: bool) -> str:
    return f"{base}/r/{sub}/top{'.json' if json_suffix else ''}"


def _search_url(base: str, subs: tuple[str, ...], json_suffix: bool) -> str:
    """Search WITHIN a set of subreddits (multireddit syntax + restrict_sr
    passed as a query param by the caller)."""
    return f"{base}/r/{'+'.join(subs)}/search{'.json' if json_suffix else ''}"


def _sitewide_search_url(base: str, json_suffix: bool) -> str:
    return f"{base}/search{'.json' if json_suffix else ''}"


def _subreddit_search_url(base: str, json_suffix: bool) -> str:
    """Community search — used to discover the topic's own subreddit(s)."""
    return f"{base}/subreddits/search{'.json' if json_suffix else ''}"


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
            meme = await _meme_from_post(client, base, json_suffix, post)
            if meme:
                return meme
    return None


async def _meme_from_post(
    client: httpx.AsyncClient, base: str, json_suffix: bool, post: dict,
) -> Meme | None:
    """Build a Meme from a chosen post dict: resolve its media and fetch
    the top comment. Shared by the feed and search paths."""
    media = resolve_media(post)
    if media is None:
        return None
    comment = author = None
    comment_media = None
    comments = await _get_json(
        client,
        _comments_url(base, post.get("permalink", ""), json_suffix),
        sort="top", limit=25, raw_json=1,
    )
    if isinstance(comments, list) and len(comments) >= 2:
        cdata = pick_top_comment(comments[1])
        if cdata:
            author = cdata.get("author") or None
            comment_media = extract_comment_media(cdata)
            comment = clean_comment_text(cdata.get("body") or "") or None
    return Meme(
        # The post's actual community, not the feed/search we found it
        # through — so the footer reads e.g. "· r/aww".
        subreddit=post.get("subreddit") or "",
        title=post.get("title") or "",
        post_author=post.get("author") or "",
        permalink=post.get("permalink") or "",
        media=media,
        comment=comment,
        comment_author=author,
        comment_media=comment_media,
    )


async def search_topic_subreddits(
    query: str,
    *,
    limit: int = 2,
    timeout: float = 12.0,
    user_agent: str | None = None,
    client_id: str = "",
    client_secret: str = "",
) -> list[str]:
    """Discover the topic's own communities via /subreddits/search — so a
    'meme about the lakers' can come from r/lakers, not just r/memes."""
    query = (query or "").strip()
    if not query:
        return []
    ua = user_agent or _USER_AGENT
    base, headers, json_suffix = await _api_context(client_id, client_secret, ua)
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, follow_redirects=True,
    ) as client:
        listing = await _get_json(
            client, _subreddit_search_url(base, json_suffix),
            q=query, limit=10, raw_json=1,
        )
    if not listing:
        return []
    return pick_topic_subreddits(listing, limit=limit)


async def candidates_from_topic_sub(
    sub: str,
    *,
    limit: int = 6,
    timeout: float = 12.0,
    user_agent: str | None = None,
    client_id: str = "",
    client_secret: str = "",
) -> list[dict]:
    """Meme-ish media posts from a topic's own community: search 'meme'
    inside it first (top of all time — the community's classics), falling
    back to meme-FLAVORED posts from its recent top listing. The plain
    top listing is full of news photos and highlight clips, so the
    fallback keeps only posts that pass is_meme_flavored (meme flair /
    'meme' in the title) rather than whatever's popular."""
    if not sub:
        return []
    ua = user_agent or _USER_AGENT
    base, headers, json_suffix = await _api_context(client_id, client_secret, ua)
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, follow_redirects=True,
    ) as client:
        listing = await _get_json(
            client, _search_url(base, (sub,), json_suffix),
            q="meme", restrict_sr="on", sort="top", t="year",
            limit=25, raw_json=1,
        )
        posts = _displayable_posts(listing, limit) if listing else []
        if not posts:
            listing = await _get_json(
                client, _listing_url(base, sub, json_suffix),
                t="month", limit=25, raw_json=1,
            )
            posts = [
                p for p in (_displayable_posts(listing) if listing else [])
                if is_meme_flavored(p)
            ][:limit]
    return posts


async def search_meme_candidates(
    query: str,
    *,
    limit: int = 8,
    timeout: float = 12.0,
    user_agent: str | None = None,
    client_id: str = "",
    client_secret: str = "",
) -> list[dict]:
    """Candidate meme posts for ``query`` from the big meme subs — first
    by relevance, then by TOP (all-time upvotes: the crowd-validated
    classics matching the query) — then a sitewide relevance search with
    'meme' appended. Deduped by permalink, listing order preserved."""
    query = (query or "").strip()
    if not query:
        return []
    ua = user_agent or _USER_AGENT
    base, headers, json_suffix = await _api_context(client_id, client_secret, ua)
    # Per-attempt take caps: relevance gets half the budget so the
    # top-voted pass always has room to contribute crowd favorites.
    attempts: list[tuple[str, dict, int]] = [
        (
            _search_url(base, MEME_SEARCH_SUBS, json_suffix),
            dict(q=query, restrict_sr="on", sort="relevance",
                 t="all", limit=50, raw_json=1),
            max(3, limit // 2),
        ),
        (
            _search_url(base, MEME_SEARCH_SUBS, json_suffix),
            dict(q=query, restrict_sr="on", sort="top",
                 t="all", limit=50, raw_json=1),
            limit,
        ),
        (
            _sitewide_search_url(base, json_suffix),
            dict(q=f"{query} meme", sort="relevance",
                 t="all", limit=50, raw_json=1),
            limit,
        ),
    ]
    out: list[dict] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, follow_redirects=True,
    ) as client:
        for url, params, take in attempts:
            if len(out) >= limit:
                break
            listing = await _get_json(client, url, **params)
            if not listing:
                continue
            for d in _displayable_posts(listing, take):
                if len(out) >= limit:
                    break
                key = d.get("permalink") or d.get("url") or ""
                if key in seen:
                    continue
                seen.add(key)
                out.append(d)
    return out[:limit]


async def meme_for_post(
    post: dict,
    *,
    timeout: float = 12.0,
    user_agent: str | None = None,
    client_id: str = "",
    client_secret: str = "",
) -> Meme | None:
    """Build a full Meme (media + top comment) from a candidate post dict
    picked by the caller."""
    ua = user_agent or _USER_AGENT
    base, headers, json_suffix = await _api_context(client_id, client_secret, ua)
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, follow_redirects=True,
    ) as client:
        return await _meme_from_post(client, base, json_suffix, post)


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
    if meme.source == "reddit":
        footer = f"\n\n· r/{meme.subreddit}"
    else:
        footer = f"\n\n· {meme.source}"
    body = (meme.comment or meme.title or "").strip()
    budget = limit - len(footer)
    if len(body) > budget:
        body = body[: budget - 1].rstrip() + "…"
    return f"{body}{footer}"
