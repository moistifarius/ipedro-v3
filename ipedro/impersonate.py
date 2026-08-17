"""Impersonation: reply in a real chat member's voice, learned from their
own message history.

When someone says "act like Luke" (or talk/sound/write like, impersonate,
pretend to be, do a Luke impression, …) the bot resolves "Luke" to an
actual member of *this* chat, pulls a sample of that member's real
messages, and builds a system prompt that turns the reply into a faithful
clone of how they write — vocabulary, casing, punctuation, emoji, typos,
attitude. Much stronger than just prepending "act like Luke" to the model.

Pure helpers (detection, member matching, prompt building) are split out
so they unit-test without a DB. The DB-touching functions are thin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ipedro.db.pool import Database

# Need at least this many sample messages to bother impersonating; below
# it there isn't enough signal and we fall through to a normal reply.
_MIN_SAMPLES = 3
# Upper bounds so the style block stays within a sane token budget.
_MAX_SAMPLES = 60
_MAX_SAMPLE_BLOCK_CHARS = 3500


@dataclass(frozen=True)
class Member:
    user_id: int
    name: str                # best display name
    first_name: str | None
    username: str | None


# ─────────────────────────────── detection ────────────────────────────────
# "do a Luke impression" / "do your best Luke" — name is BEFORE the keyword.
# Unnamed capture groups throughout (Python forbids reusing a group name in
# one pattern); detect_impersonation_request pulls the matched group from
# m.groups().
_DO_IMPRESSION_RE = re.compile(
    r"\bdo\s+(?:your\s+best\s+|an?\s+|the\s+)?(.+?)\s+"
    r"(?:impression|impersonation|imitation)\b",
    re.IGNORECASE,
)
# "pretend to be / pretend you're Luke".
_PRETEND_RE = re.compile(
    r"\bpretend\s+(?:to\s+be|(?:you|u)\s*(?:'?re|\s+are))\s+(.+)",
    re.IGNORECASE,
)
# The bread-and-butter triggers — name is AFTER the trigger phrase. Bare
# "be" is deliberately NOT a trigger: member matching is prefix-based, so
# ordinary phrases like "be nice" / "be mat" would hijack into impersonating
# a member (e.g. Matt). "become" is unambiguous enough to keep.
_TRIGGER_RE = re.compile(
    r"\b(?:act|talk|speak|sound|write|reply|respond|type)\s+(?:like|as)\s+(.+)"
    r"|\b(?:impersonate|imitate|channel|emulate|mimic)\s+(.+)"
    r"|\bbecome\s+(.+)",
    re.IGNORECASE,
)

# Strip a leading article so "act like a Luke" / "be the Luke" still resolve.
_LEADING_ARTICLE_RE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)
# Politeness / filler tails to drop off the end of a captured name. Repeated
# until none remain so "luke please now" → "luke". Multi-word names like
# "Big Joe" are untouched.
_TRAILING_FILLER_RE = re.compile(
    r"\s+(?:please|pls|plz|thanks|thx|now|for\s+me|man|dude|ok|okay|"
    r"real\s+quick|rn|lol)$",
    re.IGNORECASE,
)


def detect_impersonation_request(text: str | None) -> str | None:
    """Return the raw candidate name from an impersonation request, else None.

    The candidate may carry trailing words; member matching only looks at
    the leading token(s), so we don't over-clean here.
    """
    if not text:
        return None
    for pattern in (_DO_IMPRESSION_RE, _PRETEND_RE, _TRIGGER_RE):
        m = pattern.search(text)
        if not m:
            continue
        # _TRIGGER_RE has three alternative 'name' groups; groupdict picks
        # whichever matched (re uses the last group named 'name' it sees —
        # so collect non-None from groups() for that pattern).
        name = next((g for g in m.groups() if g), None)
        if not name:
            continue
        name = _LEADING_ARTICLE_RE.sub("", name.strip()).strip()
        # Trim trailing punctuation / connective tail ("luke, please").
        name = re.split(r"[,.!?;:]", name, maxsplit=1)[0].strip()
        # Peel off politeness/filler tails, repeatedly.
        prev = None
        while name and name != prev:
            prev = name
            name = _TRAILING_FILLER_RE.sub("", name).strip()
        if name:
            return name
    return None


def _candidate_keys(candidate: str) -> list[str]:
    """Leading-token keys to try matching against members, longest first."""
    toks = re.findall(r"[a-z0-9_]+", candidate.lower())
    keys: list[str] = []
    if len(toks) >= 2:
        keys.append(f"{toks[0]} {toks[1]}")
    if toks:
        keys.append(toks[0])
    return keys


def _member_keys(m: Member) -> set[str]:
    ks: set[str] = set()
    if m.first_name:
        ks.add(m.first_name.lower())
    if m.username:
        ks.add(m.username.lower())
    if m.name:
        ks.add(m.name.lower())
    return ks


def match_member(candidate: str, members: list[Member]) -> Member | None:
    """Resolve a candidate name string to a chat member. Exact key match
    wins; otherwise a prefix match (candidate is a prefix of a member key,
    ≥3 chars) so 'luk' → Luke. Pure/testable."""
    cand_keys = _candidate_keys(candidate)
    if not cand_keys:
        return None
    # Exact pass — try the longer (two-token) key first.
    for ck in cand_keys:
        for m in members:
            if ck in _member_keys(m):
                return m
    # Prefix pass on the first token.
    first = cand_keys[-1]
    if len(first) >= 3:
        for m in members:
            for k in _member_keys(m):
                if k.startswith(first):
                    return m
    return None


def build_impersonation_prompt(name: str, samples: list[str]) -> str:
    """The system-prompt block that turns a reply into a clone of ``name``."""
    block = "\n".join(f"- {s}" for s in samples[:_MAX_SAMPLES])
    block = block[:_MAX_SAMPLE_BLOCK_CHARS]
    return (
        f"IMPERSONATION MODE. For this reply you are NOT your usual persona "
        f"— you are impersonating a real chat member named {name}. Study "
        f"the genuine messages from {name} below and reply exactly as they "
        f"would: copy their vocabulary, slang, capitalization (lowercase? "
        f"ALL CAPS?), punctuation habits, emoji use, sentence length, "
        f"typos and verbal tics, and their attitude. Be a faithful clone, "
        f"not a caricature — don't exaggerate. Stay fully in character: "
        f"never announce that you're impersonating, never break character, "
        f"never mention being an AI or a bot. Respond to the conversation "
        f"as {name}.\n\n"
        f"Real messages from {name} (your style reference):\n{block}"
    )


# ─────────────────────────────── DB helpers ───────────────────────────────
async def chat_members(db: Database, chat_id: int) -> list[Member]:
    """Distinct members who have actually spoken in this chat."""
    rows = await db.fetch(
        """
        SELECT DISTINCT ON (m.user_id)
               m.user_id,
               COALESCE(
                   NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), ''),
                   u.username, 'user' || m.user_id::text
               ) AS name,
               u.first_name, u.username
          FROM messages m
          JOIN users u ON u.user_id = m.user_id
         WHERE m.chat_id = $1 AND m.user_id IS NOT NULL AND m.role = 'user'
        """,
        chat_id,
    )
    return [
        Member(
            user_id=r["user_id"], name=r["name"],
            first_name=r["first_name"], username=r["username"],
        )
        for r in rows
    ]


async def gather_style_samples(
    db: Database, chat_id: int, user_id: int, limit: int = _MAX_SAMPLES,
) -> list[str]:
    """Recent substantive messages from a member, newest first — the style
    reference. Skips slash-commands and trivially short lines."""
    rows = await db.fetch(
        """
        SELECT content FROM messages
         WHERE chat_id = $1 AND user_id = $2 AND role = 'user'
           AND char_length(TRIM(content)) >= 2
           AND LEFT(TRIM(content), 1) <> '/'
         ORDER BY id DESC
         LIMIT $3
        """,
        chat_id, user_id, limit,
    )
    return [r["content"].strip() for r in rows]


async def resolve_impersonation(
    db: Database, chat_id: int, text: str,
) -> tuple[Member, list[str]] | None:
    """Full pipeline: detect a request in ``text``, resolve the member,
    and gather enough style samples. Returns (member, samples) or None
    when there's no request, no matching member, or too little history.
    """
    candidate = detect_impersonation_request(text)
    if not candidate:
        return None
    members = await chat_members(db, chat_id)
    member = match_member(candidate, members)
    if member is None:
        return None
    samples = await gather_style_samples(db, chat_id, member.user_id)
    if len(samples) < _MIN_SAMPLES:
        return None
    return member, samples
