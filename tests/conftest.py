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


import pytest


@pytest.fixture(autouse=True)
def _no_ambient_dice(monkeypatch):
    """Silence on_message's random side-behaviours by default.

    Three independent coin flips ride along inside on_message: a ~4% emoji
    reaction, a ~3% Dale GIF, and a 25% "taking credit" line. Any test that
    drives the handler for some OTHER reason inherits them, so an assertion
    on how many messages were recorded, or replies sent, fails a few runs in
    a hundred — which reads as an unrelated flake and gets re-run away.

    Caught in the wild: test_catfact's record-failure test asserts exactly
    two memory writes and got a third whenever the reaction fired.

    Autouse runs before a test's own monkeypatching, so the files that
    exercise these paths deliberately (test_reactions, test_dale_sprinkle)
    still set their own value and win.
    """
    from ipedro import addressed
    from ipedro.handlers import chat
    from ipedro.memory import context_builder

    # The implicit-address window is module state; a bot reply noted by one
    # test must not make the next test's message look like a follow-up.
    addressed.reset()
    context_builder.reset_windows()
    monkeypatch.setattr(chat, "_REACT_PROBABILITY", 0.0)
    monkeypatch.setattr(chat, "_DALE_GIF_PROBABILITY", 0.0)
    monkeypatch.setattr(chat, "_CREDIT_PROBABILITY", 0.0)
