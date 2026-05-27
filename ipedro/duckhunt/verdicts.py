"""Tiny parser for AI 'VERDICT: text' single-line responses.

Used by the bef decision (ACCEPT/REFUSE) and the bef challenge judge
(PASS/FAIL). Kept dependency-free so it can be tested directly.
"""

from __future__ import annotations


def parse_verdict(text: str | None, positive: str, negative: str) -> tuple[bool | None, str]:
    """Return (verdict, body).

    verdict is True if the line started with `<positive>:`, False if it
    started with `<negative>:`, and None if the format wasn't recognised.
    `body` is the cleaned text after the colon (or the whole stripped text
    when the format wasn't recognised).
    """
    if not text:
        return None, ""
    # Take the first non-empty line so trailing model chatter doesn't trip us up.
    line = ""
    for candidate in text.splitlines():
        stripped = candidate.strip()
        if stripped:
            line = stripped
            break
    if not line:
        return None, ""

    pos = positive.upper()
    neg = negative.upper()
    upper = line.upper()
    if upper.startswith(pos + ":"):
        return True, line[len(pos) + 1:].strip().strip('"').strip()
    if upper.startswith(neg + ":"):
        return False, line[len(neg) + 1:].strip().strip('"').strip()
    return None, line
