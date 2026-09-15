"""Token counting helpers (tiktoken cl100k_base, with safe fallback)."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception as exc:  # pragma: no cover - tiktoken should normally be present
    log.warning("tiktoken unavailable (%s); using char-based approximation.", exc)
    _ENC = None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENC is None:
        # ~4 chars per token is a reasonable rough estimate.
        return max(1, len(text) // 4)
    return len(_ENC.encode(text))
