"""Other bots: the registry's rules, the supervisor that runs them, and the
owner-only commands that add them.

A bot token is a full credential for a Telegram account, so a lot of this
file is about where one may and may not end up: deleted from the chat it
was pasted in, never echoed back, never in a repr, a log line or /bots,
and handed only to that one bot's own process.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import bots, identity, persona_gen, supervisor
from ipedro.handlers import bots as bots_h

TOKEN = "7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"
DALE_TOKEN = "6000000001:AAFdaleDaleDaleDaleDaleDaleDaleDale1"
OWNER = 315660812


def _row(**over) -> bots.BotRow:
    base = dict(
        id=3, telegram_id=7123456789, username="HankBot", name="Hank",
        aliases="hank, hank hill", status="active", token=TOKEN,
        persona="You are Hank Hill.",
    )
    base.update(over)
    return bots.BotRow(**base)


def _record(row: bots.BotRow) -> dict:
    return {f: getattr(row, f) for f in bots.BotRow.__dataclass_fields__}


# ── parsing /newbot ──────────────────────────────────────────────────────────

def test_parse_newbot_reads_token_names_and_description():
    req = bots.parse_newbot(
        f"/newbot {TOKEN} Hank, Hank Hill, hank\nsells propane,\n  hates Luke's crypto talk"
    )
    assert req.token == TOKEN
    assert req.name == "Hank"
    assert req.aliases == "hank, hank hill"
    assert req.description == "sells propane, hates Luke's crypto talk"


def test_parse_newbot_without_a_description():
    assert bots.parse_newbot(f"/newbot {TOKEN} Hank").description is None


@pytest.mark.parametrize("text", [
    "/newbot", f"/newbot {TOKEN}", f"/newbot {TOKEN} , ,",
    "/newbot not-a-token Hank", "/newbot 123:short Hank",
    f"/newbot {TOKEN} {'x' * 65}",
])
def test_parse_newbot_refuses_what_it_cant_use(text):
    with pytest.raises(bots.NewBotError):
        bots.parse_newbot(text)


def test_contains_token():
    assert bots.contains_token(f"/newbot {TOKEN} Hank")
    assert not bots.contains_token("/newbot Hank")
    assert not bots.contains_token(None)


def test_nothing_repr_carries_the_token():
    """A row or request that ends up in a log line must not take the
    token (or the persona) with it."""
    assert TOKEN not in repr(_row())
    assert "Hank Hill." not in repr(_row())
    assert TOKEN not in repr(bots.parse_newbot(f"/newbot {TOKEN} Hank"))


# ── turning a row into a process ─────────────────────────────────────────────

def test_database_names_come_only_from_the_numeric_id():
    assert bots.db_name_for(7123456789) == "ipedro_bot_7123456789"
    for bad in ('ipedro_bot_1"; DROP DATABASE ipedro; --', "ipedro", "",
                "ipedro_bot_", "ipedro_bot_12a"):
        assert not bots.is_safe_db_name(bad)


def test_child_database_url_points_at_its_own_database():
    url = bots.child_database_url(
        "postgresql://u:p%40ss@postgres:5432/ipedro?sslmode=disable",
        "ipedro_bot_5",
    )
    assert url == "postgresql://u:p%40ss@postgres:5432/ipedro_bot_5?sslmode=disable"
    with pytest.raises(ValueError):
        bots.child_database_url("postgresql://u:p@h/ipedro", "x; DROP")


def test_child_env_is_its_own_bot_and_keeps_none_of_dales_secrets():
    base = {
        "TELEGRAM_BOT_TOKEN": DALE_TOKEN,
        "DATABASE_URL": "postgresql://u:p@postgres:5432/ipedro",
        "OPENAI_API_KEY": "sk-shared", "ANTHROPIC_API_KEY": "sk-ant",
        "EVOLVE_GITHUB_TOKEN": "github_pat_x",
        "evolve_github_token": "github_pat_lowercase",  # settings ignore case
        "BOT_NAME": "Dale", "MANAGES_BOTS": "true",
    }
    env = bots.child_env(base, _row(), base["DATABASE_URL"])
    assert env["TELEGRAM_BOT_TOKEN"] == TOKEN
    assert DALE_TOKEN not in env.values()
    assert env["DATABASE_URL"].endswith("/ipedro_bot_7123456789")
    assert env["BOT_NAME"] == "Hank" and env["BOT_FLAVOR"] == "plain"
    assert env["BOT_ALIASES"] == "hank, hank hill"
    assert env["BOT_PERSONA"] == "You are Hank Hill."
    assert env["MANAGES_BOTS"] == "false"
    assert env["OPENAI_API_KEY"] == "sk-shared"
    assert not any(k.upper() == "EVOLVE_GITHUB_TOKEN" for k in env)


def test_child_env_without_a_persona_sets_none():
    env = bots.child_env({}, _row(persona=None), "postgresql://u:p@h/ipedro")
    assert "BOT_PERSONA" not in env


def test_a_bot_with_no_persona_starts_as_itself_not_dale():
    plain = SimpleNamespace(bot_name="Hank", bot_flavor="plain", bot_persona=None)
    assert identity.starting_persona(plain) == "You are Hank."
    assert identity.starting_persona(SimpleNamespace()) is None   # Dale's own
    given = SimpleNamespace(bot_name="Hank", bot_flavor="plain",
                            bot_persona=" You sell propane. ")
    assert identity.starting_persona(given) == "You sell propane."


def test_manages_bots_parses_off_from_the_environment(monkeypatch):
    monkeypatch.setenv("MANAGES_BOTS", "false")
    from ipedro.config import Settings
    assert Settings(_env_file=None).manages_bots is False  # type: ignore[call-arg]
    monkeypatch.delenv("MANAGES_BOTS")
    assert Settings(_env_file=None).manages_bots is True  # type: ignore[call-arg]


def test_fingerprint_follows_what_the_process_was_started_with():
    fp = bots.fingerprint(_row())
    assert bots.fingerprint(_row(running=True, restarts=4, status="active")) == fp
    for change in (dict(token=TOKEN[:-1] + "2"), dict(name="Hank2"),
                   dict(aliases="hank"), dict(persona="You are Bobby.")):
        assert bots.fingerprint(_row(**change)) != fp


# ── the registry ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_returns_none_when_the_bot_is_already_there():
    db = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
    req = bots.parse_newbot(f"/newbot {TOKEN} Hank")
    assert await bots.register(
        db, req, telegram_id=1, username="HankBot", created_by=OWNER,
        persona="You are Hank.",
    ) is None
    sql = db.fetchrow.await_args.args[0]
    assert "WHERE bot_registry.status = 'removed'" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("key,ids", [
    ("3", [3]), ("#4", [4]), ("@hankbot", [3]), ("HANKBOT", [3]),
    ("hank", [3]), ("hank hill", [3]), ("peggy", [4]), ("bobby", []),
    ("dale", []),
])
async def test_resolve_finds_a_bot_however_the_owner_names_it(key, ids):
    rows = [_record(_row()), _record(_row(
        id=4, telegram_id=2, username="PeggyBot", name="Peggy", aliases="peggy",
    ))]
    db = SimpleNamespace(fetch=AsyncMock(return_value=rows))
    assert [b.id for b in await bots.resolve(db, key)] == ids


@pytest.mark.asyncio
async def test_set_status_refuses_made_up_states():
    with pytest.raises(ValueError):
        await bots.set_status(SimpleNamespace(), 3, "deleted")


# ── the supervisor ───────────────────────────────────────────────────────────

class FakeProc:
    _pids = iter(range(1000, 2000))

    def __init__(self, lines=()):
        self.pid = next(self._pids)
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        for line in lines:
            self.stdout.feed_data(line.encode() + b"\n")
        self._done = asyncio.Event()
        self.terminated = False

    def exit(self, code: int, *lines: str) -> None:
        for line in lines:
            self.stdout.feed_data(line.encode() + b"\n")
        self.stdout.feed_eof()
        self.returncode = code
        self._done.set()

    def terminate(self):
        self.terminated = True
        self.exit(-15)

    def kill(self):
        self.exit(-9)

    async def wait(self):
        await self._done.wait()
        return self.returncode


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def world(monkeypatch):
    """The supervisor against a registry and a process table we control."""
    state = SimpleNamespace(
        wanted=[_row()], reports=[], procs=[], envs=[], dbs=[],
        ensure_error=None, clock=Clock(),
    )

    async def wanted(db):
        return list(state.wanted)

    async def report(db, bot_id, **kw):
        state.reports.append((bot_id, kw))

    async def spawn(env):
        proc = FakeProc()
        state.procs.append(proc)
        state.envs.append(env)
        return proc

    async def ensure_db(name):
        if state.ensure_error:
            raise state.ensure_error
        state.dbs.append(name)

    monkeypatch.setattr(bots, "wanted", wanted)
    monkeypatch.setattr(bots, "report", report)
    monkeypatch.setattr(bots, "beat", AsyncMock())
    state.sup = supervisor.Supervisor(
        SimpleNamespace(), base_env={"OPENAI_API_KEY": "k"},
        base_database_url="postgresql://u:p@postgres:5432/ipedro",
        spawn=spawn, ensure_db=ensure_db, clock=state.clock,
    )
    return state


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_an_active_bot_gets_a_database_and_a_process(world):
    await world.sup.tick()
    assert world.dbs == ["ipedro_bot_7123456789"]
    assert len(world.procs) == 1
    assert world.envs[0]["TELEGRAM_BOT_TOKEN"] == TOKEN
    assert world.reports[-1] == (3, {
        "running": True, "started_at": world.reports[-1][1]["started_at"],
        "restarts": 0,
    })
    await world.sup.tick()
    assert len(world.procs) == 1          # still running: left alone


@pytest.mark.asyncio
async def test_a_crash_is_reported_scrubbed_and_restarted_with_backoff(world):
    await world.sup.tick()
    world.procs[0].exit(
        1, "Traceback (most recent call last):",
        f"aiohttp: 401 url='https://api.telegram.org/bot{TOKEN}/getMe'",
    )
    await _settle()
    await world.sup.tick()
    bot_id, kw = world.reports[-1]
    assert kw["running"] is False and kw["restarts"] == 1
    assert kw["last_exit"].startswith("exited with code 1")
    assert "Traceback" in kw["last_exit"]
    assert TOKEN not in kw["last_exit"] and "<token>" in kw["last_exit"]
    assert len(world.procs) == 1          # not straight back up

    world.clock.now += supervisor.BACKOFF_FIRST
    await world.sup.tick()
    assert len(world.procs) == 2          # back after the first backoff

    world.procs[1].exit(1)                # dies again at once: longer wait
    await _settle()
    world.clock.now += 1
    await world.sup.tick()
    world.clock.now += supervisor.BACKOFF_FIRST
    await world.sup.tick()
    assert len(world.procs) == 2
    world.clock.now += supervisor.BACKOFF_FIRST
    await world.sup.tick()
    assert len(world.procs) == 3


@pytest.mark.asyncio
async def test_a_long_traceback_never_pushes_out_the_header(world):
    await world.sup.tick()
    world.procs[0].exit(1, *[f"  frame {i} " + "x" * 200 for i in range(15)],
                        "RuntimeError: the actual reason")
    await _settle()
    await world.sup.tick()
    note = world.reports[-1][1]["last_exit"]
    assert note.startswith("exited with code 1 after 0s:")
    assert note.endswith("RuntimeError: the actual reason")
    assert len(note) <= supervisor._EXIT_NOTE_MAX


@pytest.mark.asyncio
async def test_a_long_healthy_run_resets_the_backoff(world):
    await world.sup.tick()
    child = world.sup.children[3]
    child.backoff = supervisor.BACKOFF_MAX
    world.clock.now += supervisor.STABLE_SECONDS + 1
    world.procs[0].exit(1)
    await _settle()
    await world.sup.tick()
    world.clock.now += supervisor.BACKOFF_FIRST
    await world.sup.tick()
    assert len(world.procs) == 2


@pytest.mark.asyncio
async def test_stopping_or_removing_a_bot_stops_its_process(world):
    await world.sup.tick()
    world.wanted = []
    await world.sup.tick()
    assert world.procs[0].terminated
    assert world.reports[-1] == (3, {"running": False})
    assert 3 not in world.sup.children


@pytest.mark.asyncio
async def test_changed_settings_restart_it_with_the_new_ones(world):
    await world.sup.tick()
    world.wanted = [_row(persona="You are Bobby.")]
    await world.sup.tick()
    assert world.procs[0].terminated
    assert len(world.procs) == 2
    assert world.envs[1]["BOT_PERSONA"] == "You are Bobby."


@pytest.mark.asyncio
async def test_a_database_it_cant_create_is_reported_and_retried(world):
    world.ensure_error = RuntimeError(f"permission denied (token {TOKEN})")
    await world.sup.tick()
    assert world.procs == []
    note = world.reports[-1][1]["last_exit"]
    assert note.startswith("couldn't start: permission denied")
    assert TOKEN not in note
    world.ensure_error = None
    await world.sup.tick()
    assert world.procs == []              # waits out the backoff first
    world.clock.now += supervisor.BACKOFF_FIRST
    await world.sup.tick()
    assert len(world.procs) == 1


@pytest.mark.asyncio
async def test_stop_all_takes_every_bot_down(world):
    world.wanted = [_row(), _row(id=4, telegram_id=2, token=TOKEN[:-1] + "9")]
    await world.sup.tick()
    await world.sup.stop_all()
    assert all(p.terminated for p in world.procs)
    assert world.sup.children == {}


def test_scrub_catches_tokens_mid_url_and_bare():
    assert TOKEN not in bots.scrub(f"GET /bot{TOKEN}/getUpdates")
    assert bots.scrub(f"token={TOKEN}") == "token=<token>"
    assert bots.scrub("12:30 nothing to see") == "12:30 nothing to see"


@pytest.mark.asyncio
async def test_ensure_database_creates_once_and_never_from_a_bad_name():
    db = SimpleNamespace(fetchval=AsyncMock(return_value=None), execute=AsyncMock())
    await supervisor.ensure_database(db, "ipedro_bot_5")
    db.execute.assert_awaited_once_with('CREATE DATABASE "ipedro_bot_5"')

    db = SimpleNamespace(fetchval=AsyncMock(return_value=1), execute=AsyncMock())
    await supervisor.ensure_database(db, "ipedro_bot_5")
    db.execute.assert_not_awaited()

    db = SimpleNamespace(fetchval=AsyncMock(), execute=AsyncMock())
    with pytest.raises(ValueError):
        await supervisor.ensure_database(db, 'x"; DROP DATABASE ipedro; --')
    db.fetchval.assert_not_awaited()
    db.execute.assert_not_awaited()


# ── the owner's commands ─────────────────────────────────────────────────────

def _msg(text, *, user_id=OWNER, chat_type="private", chat_id=None):
    chat = SimpleNamespace(
        id=chat_id or (user_id if chat_type == "private" else -100),
        type=chat_type, title=None,
    )
    return SimpleNamespace(
        text=text, chat=chat,
        from_user=SimpleNamespace(id=user_id, is_bot=False, username="u",
                                  first_name="U", last_name=None),
        reply=AsyncMock(), answer=AsyncMock(), delete=AsyncMock(),
    )


def _said(msg) -> list[str]:
    return [c.args[0] for c in msg.reply.await_args_list + msg.answer.await_args_list]


@pytest.fixture
def cmds(monkeypatch):
    """The bots router against a fake registry; returns its handlers."""
    state = SimpleNamespace(
        registered=[], status=[], rows=[_row()], personas=[], built=[],
        me=SimpleNamespace(id=7123456789, username="HankBot",
                           can_read_all_group_messages=True),
        heartbeat=datetime.now(timezone.utc),
    )

    async def get_me(token):
        if token != TOKEN:
            raise RuntimeError("Unauthorized")
        return state.me

    async def register(db, req, **kw):
        state.registered.append((req, kw))
        return _row(name=req.name, aliases=req.aliases, persona=kw["persona"])

    async def set_persona(db, bot_id, persona, description):
        state.personas.append((bot_id, persona, description))

    async def build_persona(rt, *, name, aliases, description):
        state.built.append((name, aliases, description))
        return persona_gen.PersonaDraft(
            f"You are {name}, written from a description.", {"Luke": 3, "crypto": 0},
            True,
        )

    async def resolve(db, key):
        k = key.strip().lstrip("#@").lower()
        return [b for b in state.rows
                if k in (str(b.id), b.name.lower(), b.username.lower())]

    async def set_status(db, bot_id, status):
        state.status.append((bot_id, status))

    async def list_bots(db):
        return list(state.rows)

    async def last_seen(db):
        return state.heartbeat

    monkeypatch.setattr(bots_h, "get_me", AsyncMock(side_effect=get_me))
    monkeypatch.setattr(bots, "register", register)
    monkeypatch.setattr(bots, "find_by_telegram_id", AsyncMock(return_value=None))
    monkeypatch.setattr(bots, "set_persona", set_persona)
    monkeypatch.setattr(persona_gen, "build_persona", build_persona)
    monkeypatch.setattr(bots, "resolve", resolve)
    monkeypatch.setattr(bots, "set_status", set_status)
    monkeypatch.setattr(bots, "list_bots", list_bots)
    monkeypatch.setattr(bots, "supervisor_last_seen", last_seen)

    rt = SimpleNamespace(
        settings=SimpleNamespace(owner_id=OWNER, telegram_bot_token=DALE_TOKEN),
        bot=SimpleNamespace(send_message=AsyncMock()),
        db=SimpleNamespace(),
        command_log=SimpleNamespace(add=AsyncMock()),
    )
    router = bots_h.build_router(rt)
    state.rt = rt
    state.h = {h.callback.__name__: h.callback
               for h in router.observers["message"].handlers}
    return state


@pytest.mark.asyncio
async def test_newbot_registers_and_never_echoes_the_token(cmds):
    msg = _msg(f"/newbot {TOKEN} Hank, hank hill\nsells propane, hates Luke")
    await cmds.h["newbot"](msg)
    msg.delete.assert_awaited_once()                 # the token is gone first
    assert cmds.built == [("Hank", "hank, hank hill", "sells propane, hates Luke")]
    req, kw = cmds.registered[0]
    assert (req.name, req.aliases, req.description) == (
        "Hank", "hank, hank hill", "sells propane, hates Luke",
    )
    assert kw == {"telegram_id": 7123456789, "username": "HankBot",
                  "created_by": OWNER,
                  "persona": "You are Hank, written from a description."}
    said = _said(msg)
    msg.reply.assert_not_awaited()                   # its message is deleted
    assert said[0].startswith("Writing Hank's persona")   # it takes a moment
    final = said[-1]
    assert "Added Hank (@HankBot) as bot #3" in final
    assert "You are Hank, written from a description." in final
    assert "Drew on what the chats remember about: Luke (3)." in final
    assert "/bot_persona #3" in final
    assert all(TOKEN not in s for s in said)
    assert TOKEN not in str(cmds.rt.command_log.add.await_args)


@pytest.mark.asyncio
async def test_a_removed_bot_comes_back_with_a_fresh_persona(cmds, monkeypatch):
    monkeypatch.setattr(bots, "find_by_telegram_id",
                        AsyncMock(return_value=_row(status="removed")))
    msg = _msg(f"/newbot {TOKEN} Hank\nback again")
    await cmds.h["newbot"](msg)
    assert len(cmds.registered) == 1 and cmds.built


@pytest.mark.asyncio
async def test_newbot_warns_when_group_privacy_is_on(cmds):
    cmds.me.can_read_all_group_messages = False
    msg = _msg(f"/newbot {TOKEN} Hank")
    await cmds.h["newbot"](msg)
    assert "Group Privacy is ON" in _said(msg)[-1]


@pytest.mark.asyncio
async def test_newbot_says_so_when_the_supervisor_isnt_running(cmds):
    cmds.heartbeat = datetime.now(timezone.utc) - timedelta(minutes=10)
    msg = _msg(f"/newbot {TOKEN} Hank")
    await cmds.h["newbot"](msg)
    assert "supervisor isn't running" in _said(msg)[-1]


@pytest.mark.asyncio
async def test_newbot_from_anyone_else_is_refused_and_their_token_removed(cmds):
    msg = _msg(f"/newbot {TOKEN} Hank", user_id=999)
    await cmds.h["newbot"](msg)
    msg.delete.assert_awaited_once()
    assert _said(msg) == ["This is owner-only."]
    assert cmds.registered == []
    bots_h.get_me.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_token_posted_in_a_group_is_deleted_and_the_owner_told(cmds):
    msg = _msg(f"/newbot {TOKEN} Hank", chat_type="supergroup")
    await cmds.h["newbot"](msg)
    msg.delete.assert_awaited_once()
    assert _said(msg) == []                          # nothing said in the group
    dm = cmds.rt.bot.send_message.await_args
    assert dm.args[0] == OWNER and "so I deleted it" in dm.args[1]
    assert TOKEN not in dm.args[1]
    assert cmds.registered == []


@pytest.mark.asyncio
async def test_a_token_that_couldnt_be_deleted_gets_a_warning(cmds):
    msg = _msg(f"/newbot {TOKEN} Hank")
    msg.delete = AsyncMock(side_effect=RuntimeError("can't"))
    await cmds.h["newbot"](msg)
    assert "couldn't delete your message" in _said(msg)[-1]


@pytest.mark.asyncio
async def test_newbot_refuses_dales_own_token(cmds):
    msg = _msg(f"/newbot {DALE_TOKEN} Hank")
    await cmds.h["newbot"](msg)
    assert _said(msg)[0].startswith("That's my own token")
    bots_h.get_me.assert_not_awaited()
    assert cmds.registered == []


@pytest.mark.asyncio
async def test_newbot_with_a_token_telegram_rejects(cmds):
    other = "7999999999:AAHzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz1"
    msg = _msg(f"/newbot {other} Hank")
    await cmds.h["newbot"](msg)
    assert "didn't accept that token" in _said(msg)[0]
    assert other not in _said(msg)[0]
    assert cmds.registered == []


@pytest.mark.asyncio
async def test_newbot_wont_replace_a_registered_bot(cmds, monkeypatch):
    monkeypatch.setattr(bots, "find_by_telegram_id",
                        AsyncMock(return_value=_row(status="stopped")))
    msg = _msg(f"/newbot {TOKEN} Hank\nsomeone new")
    await cmds.h["newbot"](msg)
    assert "already bot #3 (stopped)" in _said(msg)[0]
    assert cmds.built == [] and cmds.registered == []   # no persona written for nothing


@pytest.mark.asyncio
async def test_bot_persona_shows_the_current_one(cmds):
    cmds.rows = [_row(description="sells propane")]
    msg = _msg("/bot_persona hank")
    await cmds.h["persona_cmd"](msg)
    text = _said(msg)[0]
    assert text.startswith("#3 Hank, described as: sells propane")
    assert "You are Hank Hill." in text
    assert cmds.built == []


@pytest.mark.asyncio
async def test_bot_persona_rewrites_it_from_a_new_description(cmds):
    msg = _msg("/bot_persona #3 a   grumpy propane guy\nwho loves Luke")
    await cmds.h["persona_cmd"](msg)
    assert cmds.built == [("Hank", "hank, hank hill", "a grumpy propane guy who loves Luke")]
    assert cmds.personas == [(3, "You are Hank, written from a description.",
                              "a grumpy propane guy who loves Luke")]
    final = _said(msg)[-1]
    assert "Hank's new persona" in final and "restarts with it" in final


@pytest.mark.asyncio
async def test_bot_persona_needs_one_bot(cmds):
    msg = _msg("/bot_persona bobby a new one")
    await cmds.h["persona_cmd"](msg)
    assert "No single bot called 'bobby'" in _said(msg)[0]
    assert cmds.personas == []


@pytest.mark.asyncio
async def test_newbot_usage(cmds):
    msg = _msg("/newbot")
    await cmds.h["newbot"](msg)
    assert _said(msg)[0].startswith("Usage: /newbot")
    msg.delete.assert_not_awaited()                  # nothing secret in it


@pytest.mark.asyncio
async def test_bots_lists_state_and_never_tokens(cmds):
    cmds.rows = [
        _row(running=True, started_at=datetime.now(timezone.utc) - timedelta(hours=2)),
        _row(id=4, name="Peggy", username="PeggyBot", running=False,
             restarts=3, last_exit=f"exited with code 1 after 2s:\n{TOKEN}"),
        _row(id=5, name="Bobby", username="BobbyBot", status="stopped"),
    ]
    msg = _msg("/bots")
    await cmds.h["list_cmd"](msg)
    text = _said(msg)[0]
    assert "#3 Hank (@HankBot): running 2h" in text
    assert ("#4 Peggy (@PeggyBot): ⚠️ not running: exited with code 1 after "
            "2s — <token>, 3 restarts") in text     # why, scrubbed again
    assert "#5 Bobby (@BobbyBot): stopped" in text
    assert TOKEN not in text
    assert "Supervisor checked in" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,status", [
    ("stop_cmd", "stopped"), ("start_cmd", "active"), ("remove_cmd", "removed"),
])
async def test_stop_start_remove(cmds, handler, status):
    msg = _msg("/bot_x hank")
    await cmds.h[handler](msg)
    assert cmds.status == [(3, status)]
    assert _said(msg)[0].startswith("#3 Hank ")


@pytest.mark.asyncio
async def test_an_unknown_bot_is_named_back(cmds):
    msg = _msg("/bot_stop bobby")
    await cmds.h["stop_cmd"](msg)
    assert cmds.status == []
    assert "No bot called 'bobby'" in _said(msg)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [
    "list_cmd", "stop_cmd", "start_cmd", "remove_cmd", "persona_cmd",
])
async def test_management_is_owner_only(cmds, handler):
    msg = _msg("/bots hank", user_id=999)
    await cmds.h[handler](msg)
    assert _said(msg) == ["This is owner-only."]
    assert cmds.status == []
    group = _msg("/bots hank", chat_type="supergroup")   # even the owner
    await cmds.h[handler](group)
    assert _said(group) == []
    assert cmds.status == []


def test_child_bots_get_a_small_connection_pool():
    """Every bot adds a pool, a hub connection and a listener to one
    Postgres (default max_connections 100)."""
    env = bots.child_env({"DB_POOL_MAX": "50"}, _row(), "postgresql://u:p@h/ipedro")
    assert env["DB_POOL_MAX"] == str(bots.CHILD_DB_POOL_MAX) == "4"
