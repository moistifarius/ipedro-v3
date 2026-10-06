"""Keeps the tables that only ever grow from growing forever.

`activity_log` gets a row for every reply the bot makes AND every silence it
chooses (with the first 60 characters of the message), so it grows with the
total message volume of every chat, plus two indexes, in a database that is
backed up nightly. Nothing ever removed a row. Nothing here is needed to
answer "why did you ignore me?" past a few weeks.
"""

from __future__ import annotations

import asyncio
import logging

from ipedro.config import Settings
from ipedro.db.pool import Database

log = logging.getLogger(__name__)

_EVERY_SECONDS = 6 * 3600
_BATCH = 20_000                 # rows per DELETE, so no single statement is huge


async def prune_activity_log(db: Database, days: int) -> int:
    """Delete activity rows older than `days` days (0 or less keeps them all).
    Returns how many were removed."""
    if days <= 0:
        return 0
    total = 0
    while True:
        status = await db.execute(
            "DELETE FROM activity_log WHERE id IN ("
            "  SELECT id FROM activity_log "
            "   WHERE created_at < NOW() - make_interval(days => $1) "
            "   ORDER BY id LIMIT $2)",
            days, _BATCH,
        )
        try:
            removed = int((status or "").split()[-1])
        except (ValueError, IndexError):
            removed = 0
        total += removed
        if removed < _BATCH:
            return total


async def run_housekeeping_loop(
    db: Database, settings: Settings, stop: asyncio.Event,
) -> None:
    log.info("Housekeeping loop running (activity log kept %s days).",
             settings.activity_retention_days or "forever")
    while not stop.is_set():
        try:
            removed = await prune_activity_log(db, settings.activity_retention_days)
            if removed:
                log.info("housekeeping: pruned %d old activity rows.", removed)
        except Exception as exc:
            log.warning("housekeeping failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_EVERY_SECONDS)
        except asyncio.TimeoutError:
            pass
    log.info("Housekeeping loop stopped.")
