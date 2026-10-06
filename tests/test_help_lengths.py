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


def test_the_whole_admin_help_fits_in_one_message_too():
    from ipedro.handlers.basics import help_text_admin

    assert len(help_text_admin(True)) < 4096, "the manager's admin /help is too long"
    assert len(help_text_admin(False)) < len(help_text_admin(True))


def test_another_bots_admin_help_does_not_list_commands_it_does_not_have():
    from ipedro.handlers.basics import help_text_admin

    manager, child = help_text_admin(True), help_text_admin(False)
    for command in ("/evolve", "/newbot", "/bot_persona", "/bot_stop"):
        assert command in manager and command not in child, command
