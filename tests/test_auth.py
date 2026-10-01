"""Admin authorization tests.

The bot exposes destructive/sensitive commands; these tests pin the rules:
  - only listed user ids are admins
  - admin commands are private-DM only (no leaking to groups)
  - admin_ids ALWAYS includes 315660812
  - owner_id is its own, stricter gate — never derived from admin_ids
"""

from __future__ import annotations

from ipedro.auth import AuthContext, is_admin, is_admin_user, is_owner


ADMINS = {315660812}
OWNER = 315660812


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


def test_owner_in_private_chat_allowed():
    ctx = AuthContext(user_id=OWNER, chat_type="private")
    assert is_owner(ctx, OWNER) is True


def test_owner_in_group_denied():
    """Same private-only rule as is_admin — owner power doesn't leak to groups."""
    ctx = AuthContext(user_id=OWNER, chat_type="supergroup")
    assert is_owner(ctx, OWNER) is False


def test_an_admin_who_is_not_the_owner_is_denied():
    """The whole point of the gate: being in admin_ids is not enough."""
    ctx = AuthContext(user_id=999, chat_type="private")
    assert is_owner(ctx, OWNER) is False


def test_owner_anonymous_user_denied():
    ctx = AuthContext(user_id=None, chat_type="private")
    assert is_owner(ctx, OWNER) is False


def test_settings_owner_id_is_not_configurable_via_env(monkeypatch):
    """Unlike admin_ids, owner_id ignores ADMIN_USER_IDS entirely — it's
    not derived from the admin list at all."""
    monkeypatch.setenv("ADMIN_USER_IDS", "999")
    from ipedro.config import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.owner_id == 315660812
    assert 999 in s.admin_ids   # the admin list DID pick up the env var
    assert 999 != s.owner_id    # but the owner gate didn't move
