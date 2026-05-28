"""Centralised logging setup. Never logs secrets."""

from __future__ import annotations

import logging
import sys
from collections import deque
from typing import Deque

_REDACT_KEYS = ("token", "api_key", "openai_api_key", "telegram_bot_token", "secret")

# Ring buffer of recently-formatted log lines, used by the admin /logs command.
_RING_CAPACITY = 1000
_RING: Deque[str] = deque(maxlen=_RING_CAPACITY)


class RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _RING.append(self.format(record))
        except Exception:
            pass


def recent_log_lines(limit: int = 100, contains: str | None = None) -> list[str]:
    """Return the most recent log lines, newest last. Optional substring filter."""
    items = list(_RING)
    if contains:
        needle = contains.lower()
        items = [ln for ln in items if needle in ln.lower()]
    return items[-limit:]


class SecretRedactingFilter(logging.Filter):
    """Best-effort redaction of obvious secret values in log records."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        try:
            msg = record.getMessage()
        except Exception:
            return True
        lowered = msg.lower()
        if any(k in lowered for k in _REDACT_KEYS):
            # If a likely secret appears, scrub anything that looks like a long opaque token.
            import re

            scrubbed = re.sub(r"([A-Za-z0-9_\-]{20,})", "***REDACTED***", msg)
            record.msg = scrubbed
            record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Idempotent: clear existing handlers from a previous configure call.
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redact = SecretRedactingFilter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(redact)
    root.addHandler(handler)

    ring = RingBufferHandler()
    ring.setFormatter(formatter)
    ring.addFilter(redact)
    root.addHandler(ring)

    # aiogram and httpx are chatty at DEBUG; keep them at INFO unless explicitly DEBUG.
    if level.upper() != "DEBUG":
        logging.getLogger("aiogram").setLevel(logging.INFO)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
