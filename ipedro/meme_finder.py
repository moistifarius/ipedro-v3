"""Find a meme that's actually ABOUT the topic.

One weak search (Reddit's) + a random pick produces mostly-irrelevant
results, so the finder layers several fixes:

1. Multiple candidate queries (specific → broad → synonym) when the topic
   is derived from conversation, so one bad phrasing doesn't sink the ask.
2. KnowYourMeme query expansion: one light lookup turns the topic into
   the NAMES of meme formats about it ("distracted boyfriend", …) which
   are far better search terms than the raw topic.
3. Multi-SOURCE candidate gathering: the topic's own subreddit first,
   then the big meme subs, sitewide Reddit, Giphy (animated memes), and
   Imgur's gallery search (both optional, keyed, and skipped silently
   when unconfigured or failing).
4. An AI judge: the cheap model looks at all candidate titles and picks
   the one that is BOTH an actual meme AND about the topic — or says
   none qualify.

Pure helpers (query/pick parsing) are separated for unit tests; the
orchestration takes the openai client + source creds explicitly so it
has no Runtime dependency.
"""

from __future__ import annotations

import logging
import re

from ipedro.meme_sources import (
    giphy_candidates,
    imgur_candidates,
    imgur_top_comment,
    kym_meme_names,
)
from ipedro.prompts import (
    MEME_PICK_PROMPT, MEME_QUERIES_PROMPT, MEME_REQUEST_CLASSIFY_PROMPT,
)
from ipedro.reddit import (
    Meme,
    candidates_from_topic_sub,
    is_meme_flavored,
    meme_for_post,
    search_meme_candidates,
    search_topic_subreddits,
)

log = logging.getLogger(__name__)

_MAX_QUERIES = 3
_MAX_CANDIDATES = 16


# ───────────────────────────── pure parsing ────────────────────────────────
_LINE_PREFIX_RE = re.compile(r"^\s*(?:[-*•]|\d+\s*[.):])\s*")


def parse_topic_queries(text: str | None) -> list[str]:
    """Model output → up to 3 clean search queries. Strips numbering,
    bullets, and quotes; drops blank/runaway lines; dedupes."""
    out: list[str] = []
    for line in (text or "").splitlines():
        q = _LINE_PREFIX_RE.sub("", line.strip()).strip().strip("\"'").strip()
        if not q or len(q) > 60 or len(q.split()) > 6:
            continue
        if q.lower() in (x.lower() for x in out):
            continue
        out.append(q)
        if len(out) >= _MAX_QUERIES:
            break
    return out


def parse_pick(text: str | None, n: int) -> int | None:
    """Judge output → candidate number. 0 means 'none fit'; None means
    the answer was unusable (treat as judge-unavailable)."""
    m = re.search(r"\d+", text or "")
    if not m:
        return None
    v = int(m.group())
    if v == 0:
        return 0
    if 1 <= v <= n:
        return v
    return None


def _candidate_lines(candidates: list[dict]) -> str:
    """Numbered candidate lines for the judge — source (subreddit for
    reddit posts, 'giphy'/'imgur' otherwise), flair (a strong meme-vs-news
    signal) when present, and title."""
    lines = []
    for i, p in enumerate(candidates):
        source = p.get("source") or "reddit"
        where = f"r/{p.get('subreddit') or '?'}" if source == "reddit" else source
        flair = (p.get("link_flair_text") or "").strip()
        tag = f" [{flair}]" if flair else ""
        votes = _fmt_score(candidate_score(p))
        lines.append(
            f"{i + 1}. {where}{tag}{votes} — {p.get('title') or '(untitled)'}"
        )
    return "\n".join(lines)


def _candidate_key(p: dict) -> str:
    """Dedup key across sources: reddit permalink, else the media URL."""
    key = p.get("permalink") or p.get("url") or ""
    if not key:
        media = p.get("media")
        key = getattr(media, "url", "") if media is not None else ""
    return key


def _is_memeish(p: dict) -> bool:
    """Giphy/imgur results came from an explicit '<topic> meme' search of
    meme-heavy corpora — treat them as meme-flavored; reddit posts use the
    structural heuristic."""
    if (p.get("source") or "reddit") != "reddit":
        return True
    return is_meme_flavored(p)


# Sort weight for candidates whose source reports no vote count (Giphy):
# park them mid-pack rather than at either extreme.
_UNKNOWN_SCORE = 250
# Candidates with a KNOWN score below this are junk nobody laughed at —
# pruned when we can afford to (see gather_candidates).
_MIN_SCORE = 25
_MIN_KEEP = 4


def candidate_score(p: dict) -> int | None:
    """Upvotes for a candidate, or None when the source doesn't report
    votes (Giphy). Reddit posts carry 'score'/'ups'; imgur candidates get
    'score' from gallery points at parse time."""
    for key in ("score", "ups"):
        v = p.get(key)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def _fmt_score(score: int | None) -> str:
    if score is None:
        return ""
    if score >= 1000:
        return f" ({score / 1000:.1f}k↑)".replace(".0k", "k")
    return f" ({score}↑)"


def parse_meme_classification(raw: str | None) -> str | None:
    """Classifier output → detect_meme_request-compatible value:
    None (not a request) / '' (derive from conversation) / topic."""
    line = (raw or "").strip().splitlines()[0].strip() if (raw or "").strip() else ""
    if not line:
        return None
    upper = line.upper()
    if upper.startswith("NO"):
        return None
    if upper.startswith("THIS"):
        return ""
    if upper.startswith("TOPIC"):
        topic = line.split(":", 1)[1].strip() if ":" in line else ""
        topic = topic.strip(" \"'")
        return topic or ""
    return None


async def classify_meme_request(
    openai, text: str, chat_id: int | None = None,
) -> str | None:
    """AI fallback for meme-request detection: catches natural phrasings
    the regex grammar misses. Callers gate this on (a) the bot already
    replying to the message and (b) the word 'meme' being present, so it
    never fires on ordinary chat. Conservative by prompt: reacting to /
    discussing memes is NOT a request."""
    raw = await openai.cheap_completion(
        MEME_REQUEST_CLASSIFY_PROMPT.format(text=text[:500]),
        max_tokens=24, chat_id=chat_id,
    )
    return parse_meme_classification(raw)


# ───────────────────────────── orchestration ───────────────────────────────
async def derive_topic_queries(
    openai, chat_snippet: str, chat_id: int | None = None,
) -> list[str]:
    """Distill a conversation snippet into up to 3 search queries."""
    if not (chat_snippet or "").strip():
        return []
    raw = await openai.cheap_completion(
        MEME_QUERIES_PROMPT.format(messages=chat_snippet),
        max_tokens=60, chat_id=chat_id,
    )
    return parse_topic_queries(raw)


async def gather_candidates(
    queries: list[str],
    *,
    giphy_api_key: str = "",
    imgur_client_id: str = "",
    **reddit_creds,
) -> list[dict]:
    """Candidate posts across every configured source.

    Order: topic community (discovered from the primary query) → reddit
    meme subs / sitewide → Giphy → Imgur, for each query — including
    extra queries learned from KnowYourMeme (the NAMES of meme formats
    about the topic, which search far better than the raw topic). Keyed
    sources skip silently when unconfigured. Deduped; meme-flavored
    candidates sort first."""
    seen: set[str] = set()
    out: list[dict] = []
    ua = reddit_creds.get("user_agent") or ""

    def _take(posts: list[dict]) -> None:
        for p in posts:
            key = _candidate_key(p)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(p)

    if not queries:
        return []

    # KnowYourMeme expansion: format names beat raw topics as queries.
    kym_names = await kym_meme_names(queries[0], user_agent=ua)
    known = {q.lower() for q in queries}
    extra = [n for n in kym_names if n.lower() not in known]
    if extra:
        log.info("kym expanded %r with %r", queries[0], extra)
    all_queries = list(queries) + extra

    try:
        topic_subs = await search_topic_subreddits(queries[0], **reddit_creds)
    except Exception as exc:  # pragma: no cover - defensive
        log.info("topic-subreddit discovery failed: %s", exc)
        topic_subs = []
    for sub in topic_subs:
        if len(out) >= _MAX_CANDIDATES:
            break
        _take(await candidates_from_topic_sub(sub, **reddit_creds))

    for q in all_queries:
        if len(out) >= _MAX_CANDIDATES:
            break
        _take(await search_meme_candidates(q, **reddit_creds))
        if giphy_api_key and len(out) < _MAX_CANDIDATES:
            _take(await giphy_candidates(q, giphy_api_key, user_agent=ua))
        if imgur_client_id and len(out) < _MAX_CANDIDATES:
            _take(await imgur_candidates(q, imgur_client_id, user_agent=ua))

    # Rank: memes before non-memes, then BY VOTES within each tier — a
    # heavily-upvoted meme that's only loosely on-topic beats a precise
    # but unloved one (the judge is told the same). This also makes the
    # judge-unavailable fallback (first candidate) the most-upvoted meme.
    def _rank(p: dict) -> tuple:
        score = candidate_score(p)
        return (
            not _is_memeish(p),
            -(score if score is not None else _UNKNOWN_SCORE),
        )

    out.sort(key=_rank)
    # Prune known-low-vote junk (nobody laughed) when we can afford to —
    # never below _MIN_KEEP candidates, so niche topics still work.
    pruned = [
        p for p in out
        if (candidate_score(p) is None or candidate_score(p) >= _MIN_SCORE)
    ]
    if len(pruned) >= _MIN_KEEP:
        out = pruned
    return out[:_MAX_CANDIDATES]


async def _meme_from_candidate(
    post: dict,
    *,
    imgur_client_id: str = "",
    **reddit_creds,
) -> Meme | None:
    """Build the final Meme from the judged winner, per source. Reddit
    winners get the full treatment (top comment, comment-gif); Imgur
    winners pull the gallery's best comment; Giphy captions fall back to
    the gif title."""
    source = post.get("source") or "reddit"
    if source == "reddit":
        return await meme_for_post(post, **reddit_creds)
    if source == "imgur":
        comment = await imgur_top_comment(
            post.get("imgur_id") or "", imgur_client_id,
            user_agent=reddit_creds.get("user_agent") or "",
        )
        return Meme(
            subreddit="", title=post.get("title") or "", post_author="",
            permalink="", media=post["media"], comment=comment,
            source="imgur",
        )
    return Meme(
        subreddit="", title=post.get("title") or "", post_author="",
        permalink="", media=post["media"], source="giphy",
    )


async def find_relevant_meme(
    openai,
    queries: list[str],
    *,
    topic_label: str,
    chat_id: int | None = None,
    giphy_api_key: str = "",
    imgur_client_id: str = "",
    **reddit_creds,
) -> Meme | None:
    """The full pipeline: gather candidates, judge, build the Meme.

    The judge's 0 ("none of these are a relevant MEME") is FINAL — posting
    a news photo that happened to match the query is exactly the failure
    users notice, so an honest miss beats a wrong hit. Only when the judge
    is unavailable/unusable do we trust the (meme-flavored-first) search
    order and take the first candidate."""
    queries = [q for q in (queries or []) if (q or "").strip()]
    if not queries:
        return None
    candidates = await gather_candidates(
        queries, giphy_api_key=giphy_api_key,
        imgur_client_id=imgur_client_id, **reddit_creds,
    )
    if not candidates:
        return None
    raw = await openai.cheap_completion(
        MEME_PICK_PROMPT.format(
            topic=topic_label, candidates=_candidate_lines(candidates),
        ),
        max_tokens=8, chat_id=chat_id,
    )
    pick = parse_pick(raw, len(candidates))
    post: dict | None = None
    if pick:                      # 1..n — the judge chose one
        post = candidates[pick - 1]
    elif pick is None:            # judge unusable → trust search order
        post = candidates[0]
    if post is None:              # judge said 0 — nothing is a relevant meme
        log.info(
            "meme judge rejected all %d candidates for %r.",
            len(candidates), topic_label,
        )
        return None
    return await _meme_from_candidate(
        post, imgur_client_id=imgur_client_id, **reddit_creds,
    )
