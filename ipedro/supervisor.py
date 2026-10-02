"""Keeps the other bots running: `python -m ipedro.supervisor`.

Runs as its own compose service next to Dale (docker/docker-compose.yml),
so Dale restarting never takes the others down, and a bot crashing never
touches Dale. Every few seconds it reads the registry in Dale's database
(ipedro/bots.py) and makes reality match it: a database and a process
for every active bot, nothing for a stopped or removed one, a restart
with backoff for one that died, and a fresh start for one whose settings
changed. What it sees goes back into the registry, which is what /bots
shows the owner.

Each bot is `python -m ipedro` — this same code — started with its own
token, database and identity. Its output comes through here, prefixed
with its name, so `docker logs` on this service shows all of them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ipedro import bots
from ipedro.bots import BotRow, scrub

log = logging.getLogger(__name__)

POLL_SECONDS = 5
STOP_GRACE_SECONDS = 15
BACKOFF_FIRST = 5.0
BACKOFF_MAX = 300.0
# A run this long counts as healthy: the next crash starts the backoff over.
STABLE_SECONDS = 600
TAIL_LINES = 15
_EXIT_NOTE_MAX = 1500

@dataclass
class _Child:
    row: BotRow
    fp: str
    proc: asyncio.subprocess.Process | None = None
    pump: asyncio.Task | None = None
    started: float = 0.0
    restarts: int = 0
    backoff: float = BACKOFF_FIRST
    next_start: float = 0.0
    tail: deque = field(default_factory=lambda: deque(maxlen=TAIL_LINES))


async def spawn_bot(env: dict[str, str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "ipedro", env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )


async def ensure_database(db, name: str) -> None:
    """Create a bot's database on its first start. The name only ever
    comes from bots.db_name_for (a fixed prefix and Telegram's numeric
    id), and is checked again here because it's about to become SQL."""
    if not bots.is_safe_db_name(name):
        raise ValueError(f"refusing database name {name!r}")
    if await db.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
        return
    await db.execute(f'CREATE DATABASE "{name}"')
    log.info("created database %s", name)


class Supervisor:
    def __init__(
        self, db, *, base_env, base_database_url: str,
        spawn=spawn_bot, ensure_db=None, clock=time.monotonic,
    ) -> None:
        self.db = db
        self.base_env = dict(base_env)
        self.base_database_url = base_database_url
        self._spawn = spawn
        self._ensure_db = ensure_db or (lambda name: ensure_database(db, name))
        self._clock = clock
        self.children: dict[int, _Child] = {}

    async def tick(self) -> None:
        rows = {r.id: r for r in await bots.wanted(self.db)}
        for bot_id in [i for i in self.children if i not in rows]:
            await self._stop(bot_id, "stopped by the owner")
        for row in rows.values():
            fp = bots.fingerprint(row)
            child = self.children.get(row.id)
            if child is not None and child.fp != fp:
                await self._stop(row.id, "restarting with new settings")
                child = None
            if child is None:
                child = self.children[row.id] = _Child(row=row, fp=fp)
            child.row = row
            if child.proc is not None and child.proc.returncode is not None:
                await self._note_exit(child)
            if child.proc is None and self._clock() >= child.next_start:
                await self._start(child)
        await bots.beat(self.db)

    async def stop_all(self) -> None:
        for bot_id in list(self.children):
            try:
                await self._stop(bot_id, "supervisor shutting down")
            except Exception:
                log.warning("stopping bot #%s failed", bot_id, exc_info=True)

    def _schedule_retry(self, child: _Child) -> None:
        child.next_start = self._clock() + child.backoff
        child.backoff = min(child.backoff * 2, BACKOFF_MAX)

    async def _start(self, child: _Child) -> None:
        row = child.row
        try:
            await self._ensure_db(bots.db_name_for(row.telegram_id))
            env = bots.child_env(self.base_env, row, self.base_database_url)
            child.proc = await self._spawn(env)
        except Exception as exc:
            child.proc = None
            self._schedule_retry(child)
            note = scrub(f"couldn't start: {exc}", row.token)[:_EXIT_NOTE_MAX]
            log.warning("bot #%s (%s) %s", row.id, row.name, note)
            await bots.report(self.db, row.id, running=False, last_exit=note)
            return
        child.started = self._clock()
        child.tail.clear()
        child.pump = asyncio.create_task(
            self._pump(child, child.proc), name=f"bot-{row.id}-output",
        )
        log.info(
            "bot #%s (%s, @%s) started, pid %s",
            row.id, row.name, row.username, child.proc.pid,
        )
        await bots.report(
            self.db, row.id, running=True,
            started_at=datetime.now(timezone.utc), restarts=child.restarts,
        )

    async def _pump(self, child: _Child, proc) -> None:
        """Relay a bot's output, scrubbed, until it exits. Keeps reading no
        matter what: a bot whose pipe nobody drains blocks on its next
        write and hangs."""
        name, token = child.row.name, child.row.token
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:          # one enormous line; the next one's fine
                continue
            if not raw:
                break
            line = scrub(raw.decode(errors="replace").rstrip(), token)
            if line:
                child.tail.append(line)
                log.info("[%s] %s", name, line)
        await proc.wait()

    async def _note_exit(self, child: _Child) -> None:
        code = child.proc.returncode
        if child.pump is not None:
            await asyncio.wait({child.pump}, timeout=2)   # let the tail drain
        ran = self._clock() - child.started
        child.proc = child.pump = None
        child.restarts += 1
        if ran >= STABLE_SECONDS:
            child.backoff = BACKOFF_FIRST
        delay = child.backoff
        self._schedule_retry(child)
        tail = "\n".join(child.tail)
        note = f"exited with code {code} after {int(ran)}s"
        if tail:
            note += ":\n" + tail
        log.warning(
            "bot #%s (%s) %s; restarting in %ds",
            child.row.id, child.row.name, note.splitlines()[0].rstrip(":"), delay,
        )
        await bots.report(
            self.db, child.row.id, running=False, restarts=child.restarts,
            last_exit=note[-_EXIT_NOTE_MAX:],
        )

    async def _stop(self, bot_id: int, why: str) -> None:
        child = self.children.pop(bot_id, None)
        if child is None:
            return
        proc = child.proc
        if proc is not None and proc.returncode is None:
            log.info("bot #%s (%s): %s", bot_id, child.row.name, why)
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), STOP_GRACE_SECONDS)
            except ProcessLookupError:
                pass
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if child.pump is not None:
            child.pump.cancel()
        await bots.report(self.db, bot_id, running=False)


async def _wait_for_registry(db, stop: asyncio.Event) -> None:
    """Dale creates the registry table when he starts; until then there's
    nothing to supervise."""
    said = False
    while not stop.is_set():
        if await db.fetchval("SELECT to_regclass('public.bot_registry')"):
            return
        if not said:
            log.info("waiting for Dale to create the bot registry")
            said = True
        try:
            await asyncio.wait_for(stop.wait(), POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def run() -> None:
    from ipedro.config import get_settings
    from ipedro.db.pool import Database
    from ipedro.logging_setup import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("Bot supervisor starting.")
    db = await Database.connect(settings.database_url, min_size=1, max_size=3)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - windows
            pass

    sup = Supervisor(
        db, base_env=os.environ, base_database_url=settings.database_url,
    )
    try:
        await _wait_for_registry(db, stop)
        while not stop.is_set():
            try:
                await sup.tick()
            except Exception:
                log.exception("supervisor tick failed")
            try:
                await asyncio.wait_for(stop.wait(), POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        await sup.stop_all()
        await db.close()
        log.info("Bot supervisor stopped.")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
