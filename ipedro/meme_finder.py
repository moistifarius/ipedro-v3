"""Find a meme that's actually ABOUT the topic.

Reddit's search is weak, so a single literal query + random pick among
hits produces mostly-irrelevant results. This module layers three fixes:

1. Multiple candidate queries (specific → broad → synonym) when the topic
   is derived from conversation, so one bad phrasing doesn't sink the ask.
2. Multi-source candidate gathering, topic community FIRST: discover the
   topic's own subreddit(s) via /subreddits/search and pull meme-ish posts
   from there, then the big meme subs, then sitewide.
3. An AI judge: the cheap model looks at the candidate post titles and
   picks the one that genuinely matches the topic — or says none do —
   instead of trusting Reddit's relevance order blindly.

Pure helpers (query/pick parsing) are separated for unit tests; the
orchestration takes the openai client + reddit creds explicitly so it has
no Runtime dependency.
"""

from __future__ import annotations

import logging
import re

from ipedro.prompts import MEME_PICK_PROMPT, MEME_QUERIES_PROMPT
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
_MAX_CANDIDATES = 12


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
    """Numbered candidate lines for the judge — subreddit, flair (a strong
    meme-vs-news signal) when present, and title."""
    lines = []
    for i, p in enumerate(candidates):
        flair = (p.get("link_flair_text") or "").strip()
        tag = f" [{flair}]" if flair else ""
        lines.append(
            f"{i + 1}. r/{p.get('subreddit') or '?'}{tag} — "
            f"{p.get('title') or '(untitled)'}"
        )
    return "\n".join(lines)


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


async def gather_candidates(queries: list[str], **creds) -> list[dict]:
    """Candidate posts across sources, topic community first.

    The primary (most specific) query drives subreddit discovery; every
    query then feeds the meme-sub + sitewide searches until the cap."""
    seen: set[str] = set()
    out: list[dict] = []

    def _take(posts: list[dict]) -> None:
        for p in posts:
            key = p.get("permalink") or p.get("url") or ""
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(p)

    if queries:
        try:
            topic_subs = await search_topic_subreddits(queries[0], **creds)
        except Exception as exc:  # pragma: no cover - defensive
            log.info("topic-subreddit discovery failed: %s", exc)
            topic_subs = []
        for sub in topic_subs:
            if len(out) >= _MAX_CANDIDATES:
                break
            _take(await candidates_from_topic_sub(sub, **creds))
    for q in queries:
        if len(out) >= _MAX_CANDIDATES:
            break
        _take(await search_meme_candidates(q, **creds))
    # Sink non-meme-looking posts (news photos, highlight clips from the
    # sitewide sweep) below actual memes. Stable, so within each tier the
    # topic-sub → meme-sub → sitewide priority is preserved. This also
    # makes the judge-unavailable fallback (first candidate) a meme.
    out.sort(key=lambda p: not is_meme_flavored(p))
    return out[:_MAX_CANDIDATES]


async def find_relevant_meme(
    openai,
    queries: list[str],
    *,
    topic_label: str,
    chat_id: int | None = None,
    **creds,
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
    candidates = await gather_candidates(queries, **creds)
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
    return await meme_for_post(post, **creds)
