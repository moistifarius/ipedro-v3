"""Test fixtures and stubs.

Tests must not require real Postgres, Telegram, or OpenAI. The fakes here
mimic just enough surface area for unit tests.
"""

from __future__ import annotations

import os

# Provide harmless env values so pydantic-settings doesn't refuse to import
# config.Settings when individual tests touch it.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
