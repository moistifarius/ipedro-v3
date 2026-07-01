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
    return "\n".join(
        f"{i + 1}. r/{p.get('subreddit') or '?'} — {p.get('title') or '(untitled)'}"
        for i, p in enumerate(candidates)
    )


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
    return out[:_MAX_CANDIDATES]


async def find_relevant_meme(
    openai,
    queries: list[str],
    *,
    topic_label: str,
    trust_first: bool = False,
    chat_id: int | None = None,
    **creds,
) -> Meme | None:
    """The full pipeline: gather candidates, judge, build the Meme.

    ``trust_first``: when the judge says none fit (or is unavailable),
    fall back to the first candidate anyway — used for EXPLICIT topics,
    where the search already matched the user's literal words. For derived
    topics we return None instead so the caller can be honest about it."""
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
    elif trust_first:             # judge said 0, but the topic was explicit
        post = candidates[0]
    if post is None:
        log.info(
            "meme judge rejected all %d candidates for %r.",
            len(candidates), topic_label,
        )
        return None
    return await meme_for_post(post, **creds)
