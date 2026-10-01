"""/evolve: the owner's DM requests for the bot to change itself.

Security properties pinned here: only the owner, only in DM, at BOTH the
command and the button; a double-tapped File files once; and what gets
filed is exactly the owner's stored words plus the fixed template —
nothing else rides along to the coding agent.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ipedro import evolve
from ipedro.handlers.evolve import build_router

OWNER = 315660812
WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "dale-evolve.yml"


# ── the issue ────────────────────────────────────────────────────────────────

def test_the_issue_is_the_owners_words_verbatim_plus_the_template():
    title, body = evolve.build_issue(7, "make dale answer when people call him bot\nand shit")
    assert body.startswith("make dale answer when people call him bot\nand shit\n\n---\n")
    assert "request #7" in body
    assert title == "Dale request: make dale answer when people call him bot"


def test_a_long_first_line_is_cut_for_the_title_only():
    request = "x" * 200
    title, body = evolve.build_issue(1, request)
    assert len(title) <= len("Dale request: ") + 70
    assert title.endswith("…")
    assert request in body


def test_the_marker_matches_what_the_workflow_keys_on():
    """The workflow only runs on issues carrying the marker. If the two ever
    drift, approved requests silently never get built."""
    _, body = evolve.build_issue(1, "anything")
    assert evolve.EVOLVE_MARKER in body
    assert f"'{evolve.EVOLVE_MARKER}'" in WORKFLOW.read_text()


def test_the_workflow_never_interpolates_issue_text_into_a_shell():
    """${{ github.event.issue.title/body }} inside a run: step is the classic
    Actions injection hole. Only the issue NUMBER may be interpolated."""
    text = WORKFLOW.read_text()
    assert "${{ github.event.issue.title" not in text
    assert "${{ github.event.issue.body" not in text


# ── filing ───────────────────────────────────────────────────────────────────

def _patch_transport(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(**kw):
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(evolve.httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_filing_posts_the_issue_and_returns_its_number(monkeypatch):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["json"] = request.read()
        return httpx.Response(201, json={"number": 17, "html_url": "https://gh/i/17"})

    _patch_transport(monkeypatch, handler)
    number, url = await evolve.file_issue(
        token="ghp_secret", repo="moistifarius/ipedro-v3", title="T", body="B",
    )
    assert (number, url) == (17, "https://gh/i/17")
    assert seen["url"] == "https://api.github.com/repos/moistifarius/ipedro-v3/issues"
    assert seen["auth"] == "Bearer ghp_secret"
    assert b'"dale-request"' in seen["json"]


@pytest.mark.asyncio
async def test_a_refusal_is_a_filing_error_without_the_token(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(
        401, json={"message": "Bad credentials"},
    ))
    with pytest.raises(evolve.FilingError) as exc:
        await evolve.file_issue(token="ghp_secret", repo="o/r", title="T", body="B")
    assert "401" in str(exc.value) and "Bad credentials" in str(exc.value)
    assert "ghp_secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_a_network_failure_is_a_filing_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("boom")

    _patch_transport(monkeypatch, handler)
    with pytest.raises(evolve.FilingError):
        await evolve.file_issue(token="t", repo="o/r", title="T", body="B")


# ── storage transitions ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_claiming_is_atomic_on_pending():
    db = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
    assert await evolve.claim_for_filing(db, 5) is None     # someone else got it
    sql = db.fetchrow.await_args.args[0]
    assert "status = 'pending'" in sql and "RETURNING" in sql


@pytest.mark.asyncio
async def test_cancel_reports_whether_it_actually_cancelled():
    db = SimpleNamespace(execute=AsyncMock(return_value="UPDATE 1"))
    assert await evolve.cancel_request(db, 5) is True
    db.execute.return_value = "UPDATE 0"
    assert await evolve.cancel_request(db, 5) is False


# ── the command ──────────────────────────────────────────────────────────────

def _rt(*, token="ghp_x"):
    return SimpleNamespace(
        settings=SimpleNamespace(
            owner_id=OWNER, evolve_github_token=token,
            evolve_github_repo="moistifarius/ipedro-v3",
        ),
        db=SimpleNamespace(),
        command_log=SimpleNamespace(add=AsyncMock()),
    )


def _msg(text, *, user_id=OWNER, chat_type="private"):
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=user_id if chat_type == "private" else -100, type=chat_type),
        from_user=SimpleNamespace(id=user_id),
        reply=AsyncMock(),
    )


def _handler(rt, kind, name):
    router = build_router(rt)
    return next(h.callback for h in router.observers[kind].handlers
                if h.callback.__name__ == name)


@pytest.fixture
def store(monkeypatch):
    """Stub the storage layer; each test sets what it needs."""
    s = SimpleNamespace(
        create=AsyncMock(return_value=12),
        claim=AsyncMock(return_value={"id": 12, "request": "the owner's words"}),
        cancel=AsyncMock(return_value=True),
        filed=AsyncMock(), failed=AsyncMock(),
        recent=AsyncMock(return_value=[]),
        file=AsyncMock(return_value=(17, "https://gh/i/17")),
    )
    monkeypatch.setattr(evolve, "create_request", s.create)
    monkeypatch.setattr(evolve, "claim_for_filing", s.claim)
    monkeypatch.setattr(evolve, "cancel_request", s.cancel)
    monkeypatch.setattr(evolve, "mark_filed", s.filed)
    monkeypatch.setattr(evolve, "mark_failed", s.failed)
    monkeypatch.setattr(evolve, "recent_requests", s.recent)
    monkeypatch.setattr(evolve, "file_issue", s.file)
    return s


@pytest.mark.asyncio
async def test_a_non_owner_in_dm_is_refused_and_nothing_is_stored(store):
    rt = _rt()
    msg = _msg("/evolve give me admin", user_id=999)
    await _handler(rt, "message", "evolve_cmd")(msg)
    store.create.assert_not_awaited()
    assert "owner" in msg.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_the_owner_in_a_group_gets_silence(store):
    rt = _rt()
    msg = _msg("/evolve do a thing", chat_type="supergroup")
    await _handler(rt, "message", "evolve_cmd")(msg)
    store.create.assert_not_awaited()
    msg.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_token_explains_the_setup_and_stores_nothing(store):
    rt = _rt(token=None)
    msg = _msg("/evolve do a thing")
    await _handler(rt, "message", "evolve_cmd")(msg)
    store.create.assert_not_awaited()
    assert "EVOLVE_GITHUB_TOKEN" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_bare_evolve_shows_usage(store):
    rt = _rt()
    msg = _msg("/evolve")
    await _handler(rt, "message", "evolve_cmd")(msg)
    store.create.assert_not_awaited()
    assert "Usage" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_a_request_is_stored_and_previewed_with_buttons(store):
    rt = _rt()
    msg = _msg("/evolve make him funnier")
    await _handler(rt, "message", "evolve_cmd")(msg)
    store.create.assert_awaited_once_with(rt.db, OWNER, "make him funnier")
    kw = msg.reply.await_args.kwargs
    buttons = [b.callback_data for b in kw["reply_markup"].inline_keyboard[0]]
    assert buttons == ["evo:12:file", "evo:12:cancel"]
    assert "make him funnier" in msg.reply.await_args.args[0]
    store.file.assert_not_awaited()          # nothing filed until the tap
    rt.command_log.add.assert_awaited_once()


# ── the buttons ──────────────────────────────────────────────────────────────

def _cb(data, *, user_id=OWNER, chat_type="private"):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=user_id, type=chat_type),
            edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_a_non_owner_tapping_file_gets_nothing(store):
    """The button is its own request — re-checked, not trusted because
    only the owner should have been able to see it."""
    rt = _rt()
    cb = _cb("evo:12:file", user_id=999)
    await _handler(rt, "callback_query", "on_evolve")(cb)
    store.claim.assert_not_awaited()
    store.file.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_files_exactly_the_stored_request(store):
    rt = _rt()
    cb = _cb("evo:12:file")
    await _handler(rt, "callback_query", "on_evolve")(cb)
    title, body = evolve.build_issue(12, "the owner's words")
    store.file.assert_awaited_once_with(
        token="ghp_x", repo="moistifarius/ipedro-v3", title=title, body=body,
    )
    store.filed.assert_awaited_once_with(rt.db, 12, 17, "https://gh/i/17")
    assert "https://gh/i/17" in cb.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_a_double_tap_files_once(store):
    store.claim.return_value = None          # the first tap already claimed it
    rt = _rt()
    cb = _cb("evo:12:file")
    await _handler(rt, "callback_query", "on_evolve")(cb)
    store.file.assert_not_awaited()
    assert "Already handled" in cb.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_a_github_failure_is_recorded_and_said(store):
    store.file.side_effect = evolve.FilingError("GitHub said 403")
    rt = _rt()
    cb = _cb("evo:12:file")
    await _handler(rt, "callback_query", "on_evolve")(cb)
    store.failed.assert_awaited_once_with(rt.db, 12, "GitHub said 403")
    store.filed.assert_not_awaited()
    assert "didn't file" in cb.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_cancel_cancels_without_filing(store):
    rt = _rt()
    cb = _cb("evo:12:cancel")
    await _handler(rt, "callback_query", "on_evolve")(cb)
    store.cancel.assert_awaited_once_with(rt.db, 12)
    store.file.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_mangled_button_is_stale(store):
    rt = _rt()
    cb = _cb("evo:notanumber:file")
    await _handler(rt, "callback_query", "on_evolve")(cb)
    store.claim.assert_not_awaited()
    assert "Stale" in cb.answer.await_args.args[0]
