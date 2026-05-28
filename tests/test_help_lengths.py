"""Sanity tests: HELP_TEXT_* must fit in a single Telegram outbound message
(4096-char cap)."""

from __future__ import annotations

from ipedro.handlers.basics import HELP_TEXT_ADMIN, HELP_TEXT_PUBLIC


def test_help_text_public_under_4000():
    assert len(HELP_TEXT_PUBLIC) < 4000, (
        f"HELP_TEXT_PUBLIC is {len(HELP_TEXT_PUBLIC)} chars; trim it."
    )


def test_help_text_admin_under_4000():
    assert len(HELP_TEXT_ADMIN) < 4000, (
        f"HELP_TEXT_ADMIN is {len(HELP_TEXT_ADMIN)} chars; trim it."
    )
