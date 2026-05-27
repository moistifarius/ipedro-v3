"""One-shot migrator: import legacy iPedro V1 flat files into Postgres.

Usage:
    python -m scripts.migrate_legacy --chat-ids ../iPedro/iPedro/data/chat_ids \
                                     --duckpoints ../iPedro/iPedro/data/duckpoint \
                                     --chat-history ../iPedro/iPedro/data/chat_history

Only files that exist are imported. The migrator is idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from ipedro.config import get_settings
from ipedro.db.migrations import apply_schema
from ipedro.db.pool import Database
from ipedro.db.repositories import ChatRepo
from ipedro.logging_setup import configure_logging

log = logging.getLogger(__name__)


async def _import_chat_ids(db: Database, path: Path) -> int:
    chats = ChatRepo(db)
    count = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            chat_id = int(line)
        except ValueError:
            continue
        # Heuristic: negative ids = group/supergroup, positive = private.
        chat_type = "supergroup" if chat_id < 0 else "private"
        await chats.upsert_chat(chat_id, chat_type, None)
        count += 1
    log.info("Imported %d legacy chat ids from %s", count, path)
    return count


async def _import_duckpoints(db: Database, path: Path, default_chat_id: int) -> int:
    """Legacy duckpoint format: 'user_id,display_name,action' per line."""
    count = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            user_id_s, display, action = line.split(",", 2)
            user_id = int(user_id_s.strip())
        except ValueError:
            continue
        col = {
            "bang": "killed",
            "bef": "befriended",
            "ignore": "ignored",
            "berate": "berated_win",
        }.get(action.strip().lower())
        if not col:
            continue
        await db.execute(
            f"""
            INSERT INTO duck_stats (chat_id, user_id, display_name, {col}, points)
            VALUES ($1, $2, $3, 1, 1)
            ON CONFLICT (chat_id, user_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                {col} = duck_stats.{col} + 1,
                points = duck_stats.points + 1
            """,
            default_chat_id, user_id, display.strip(),
        )
        count += 1
    log.info("Imported %d legacy duckpoint rows from %s", count, path)
    return count


async def _import_chat_history(db: Database, path: Path, chat_id: int) -> int:
    """Legacy chat_history is just newline-delimited free text. Best-effort import."""
    count = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        await db.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES ($1, 'user', $2)",
            chat_id, line,
        )
        count += 1
    log.info("Imported %d legacy chat history lines into chat %s", count, chat_id)
    return count


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat-ids", type=Path)
    parser.add_argument("--duckpoints", type=Path)
    parser.add_argument("--chat-history", type=Path)
    parser.add_argument(
        "--default-chat-id",
        type=int,
        default=-1001273502662,
        help="Chat id to attribute legacy unattributed duck/history records to.",
    )
    args = parser.parse_args()

    configure_logging("INFO")
    settings = get_settings()
    db = await Database.connect(settings.database_url)
    await apply_schema(db, settings.openai_embedding_dim)
    try:
        if args.chat_ids and args.chat_ids.exists():
            await _import_chat_ids(db, args.chat_ids)
        # Make sure default chat exists before importing its data.
        chats = ChatRepo(db)
        await chats.upsert_chat(args.default_chat_id, "supergroup", "imported-legacy")
        if args.duckpoints and args.duckpoints.exists():
            await _import_duckpoints(db, args.duckpoints, args.default_chat_id)
        if args.chat_history and args.chat_history.exists():
            await _import_chat_history(db, args.chat_history, args.default_chat_id)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
