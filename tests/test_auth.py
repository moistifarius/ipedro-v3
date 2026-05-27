"""Admin authorization tests.

The bot exposes destructive/sensitive commands; these tests pin the rules:
  - only listed user ids are admins
  - admin commands are private-DM only (no leaking to groups)
  - admin_ids ALWAYS includes 315660812
"""

from __future__ import annotations

from ipedro.auth import AuthContext, is_admin, is_admin_user


ADMINS = {315660812}


def test_admin_in_private_chat_allowed():
    ctx = AuthContext(user_id=315660812, chat_type="private")
    assert is_admin(ctx, ADMINS) is True


def test_admin_in_group_denied():
    ctx = AuthContext(user_id=315660812, chat_type="supergroup")
    assert is_admin(ctx, ADMINS) is False


def test_non_admin_in_private_denied():
    ctx = AuthContext(user_id=999, chat_type="private")
    assert is_admin(ctx, ADMINS) is False


def test_anonymous_user_denied():
    ctx = AuthContext(user_id=None, chat_type="private")
    assert is_admin(ctx, ADMINS) is False


def test_user_check_is_lenient_about_chat_type():
    assert is_admin_user(315660812, ADMINS) is True
    assert is_admin_user(999, ADMINS) is False


def test_settings_always_includes_315660812(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    from ipedro.config import Settings

    s = Settings()  # type: ignore[call-arg]
    assert 315660812 in s.admin_ids


def test_settings_admin_ids_parses_extras(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "1, 2 ,3,not-an-id, ")
    from ipedro.config import Settings

    s = Settings()  # type: ignore[call-arg]
    assert {1, 2, 3, 315660812}.issubset(s.admin_ids)
