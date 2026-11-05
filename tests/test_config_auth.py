"""Authorization for the /config wizard + cfg: callback (regression for an
audit finding: the callback had no auth check, so any group member could
flip a chat's settings, and a crafted callback could target other chats)."""

from __future__ import annotations

import pytest

from ipedro.handlers.utility import _can_edit_config


class _Chat:
    def __init__(self, chat_id, ctype="supergroup"):
        self.id = chat_id
        self.type = ctype


class _Member:
    def __init__(self, status):
        self.status = status


class _Bot:
    def __init__(self, status_by_user):
        self._status = status_by_user

    async def get_chat_member(self, chat_id, user_id):
        if user_id not in self._status:
            raise RuntimeError("not a member")
        return _Member(self._status[user_id])


class _Settings:
    def __init__(self, admin_ids):
        self.admin_ids = frozenset(admin_ids)


class _RT:
    def __init__(self, admin_ids=(), status_by_user=None):
        self.settings = _Settings(admin_ids)
        self.bot = _Bot(status_by_user or {})


@pytest.mark.asyncio
async def test_bot_admin_allowed_anywhere_any_target():
    rt = _RT(admin_ids={7})
    # Bot admin editing a DIFFERENT chat from their DM (the /config_for flow).
    assert await _can_edit_config(rt, 7, _Chat(999, "private"), -100123) is True


@pytest.mark.asyncio
async def test_chat_admin_allowed_for_own_chat():
    rt = _RT(status_by_user={5: "administrator"})
    host = _Chat(-100123)
    assert await _can_edit_config(rt, 5, host, -100123) is True


@pytest.mark.asyncio
async def test_non_admin_member_rejected():
    rt = _RT(status_by_user={5: "member"})
    host = _Chat(-100123)
    assert await _can_edit_config(rt, 5, host, -100123) is False


@pytest.mark.asyncio
async def test_chat_admin_cannot_target_other_chat():
    # Admin of the host chat, but the callback targets a DIFFERENT chat —
    # the crafted-callback cross-chat vector must be blocked.
    rt = _RT(status_by_user={5: "creator"})
    host = _Chat(-100123)
    assert await _can_edit_config(rt, 5, host, -100999) is False


@pytest.mark.asyncio
async def test_none_user_and_dm_non_admin_rejected():
    rt = _RT(admin_ids={7}, status_by_user={5: "creator"})
    assert await _can_edit_config(rt, None, _Chat(-100123), -100123) is False
    # A DM (private) has no chat admins → only bot admins pass there.
    assert await _can_edit_config(rt, 5, _Chat(5, "private"), 5) is False


# ── "clear custom" in the wizard ─────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("before,expect_persona", [
    ("pirate", "dude"),            # a custom persona's name is only a label
    ("dude", None),                # a built-in stays as it is
    ("neutral", None),
])
async def test_clearing_a_custom_persona_leaves_a_persona_that_exists(before, expect_persona):
    """It nulled the text but kept the custom NAME, so the chat quietly ran
    the master prompt while the header still said "Persona: pirate"."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ipedro.handlers.utility import build_router

    cfg = SimpleNamespace(
        persona=before, persona_custom=None, response_policy="mention",
        ambient_probability=0.03, duckhunt_enabled=False, share_photo_enabled=False,
        comic_enabled=False, fortune_enabled=False, voice_transcribe=True,
        memory_enabled=True, ether_enabled=False, duck_names_public=True,
        monthly_recap_enabled=True, automod_enabled=True, vision_enabled=True,
        on_this_day_enabled=True,
    )
    rt = SimpleNamespace(
        settings=SimpleNamespace(admin_ids=frozenset({7})),
        chats=SimpleNamespace(get_config=AsyncMock(return_value=cfg), update_config=AsyncMock()),
        bot=SimpleNamespace(),
    )
    on_cfg = next(h.callback for h in build_router(rt).observers["callback_query"].handlers
                  if h.callback.__name__ == "on_cfg")
    cb = SimpleNamespace(
        data="cfg:-100123:custompersona:clear",
        from_user=SimpleNamespace(id=7),
        message=SimpleNamespace(chat=SimpleNamespace(id=7, type="private"),
                                edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await on_cfg(cb)
    sent = rt.chats.update_config.await_args
    assert sent.args == (-100123,)
    assert sent.kwargs["persona_custom"] is None
    assert sent.kwargs.get("persona") == expect_persona
