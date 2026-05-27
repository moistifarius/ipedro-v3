"""Centralised logging setup. Never logs secrets."""

from __future__ import annotations

import logging
import sys

_REDACT_KEYS = ("token", "api_key", "openai_api_key", "telegram_bot_token", "secret")


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

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    handler.addFilter(SecretRedactingFilter())
    root.addHandler(handler)

    # aiogram and httpx are chatty at DEBUG; keep them at INFO unless explicitly DEBUG.
    if level.upper() != "DEBUG":
        logging.getLogger("aiogram").setLevel(logging.INFO)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
