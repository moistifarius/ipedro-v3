"""Bot lifecycle: build the aiogram bot/dispatcher, wire routers, run."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from ipedro.config import Settings, get_settings
from ipedro.db.migrations import apply_schema, has_pgvector
from ipedro.db.pool import Database, set_db
from ipedro.db.repositories import ChatRepo, CommandLogRepo, UserRepo
from ipedro.duckhunt.service import DuckhuntService
from ipedro.duckhunt.spawner import run_spawner
from ipedro.handlers import admin as admin_h
from ipedro.handlers import ai as ai_h
from ipedro.handlers import basics as basics_h
from ipedro.handlers import chat as chat_h
from ipedro.handlers import debug as debug_h
from ipedro.handlers import duckhunt as duck_h
from ipedro.logging_setup import configure_logging
from ipedro.memory.store import MemoryStore
from ipedro.openai_client import OpenAIClient
from ipedro.runtime import Runtime
from ipedro.sharephoto import run_share_photo_loop

log = logging.getLogger(__name__)


async def build_runtime(settings: Settings) -> Runtime:
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    db = await Database.connect(settings.database_url)
    set_db(db)
    await apply_schema(db, settings.openai_embedding_dim)
    pgvector_available = await has_pgvector(db)

    openai = OpenAIClient(
        api_key=settings.openai_api_key,
        organization=settings.openai_organization,
        text_model=settings.openai_text_model,
        image_model=settings.openai_image_model,
        transcription_model=settings.openai_transcription_model,
        embedding_model=settings.openai_embedding_model,
        embedding_dim=settings.openai_embedding_dim,
    )
    memory = MemoryStore(db=db, openai=openai, pgvector_available=pgvector_available)
    return Runtime(
        settings=settings,
        bot=bot,
        db=db,
        openai=openai,
        memory=memory,
        duckhunt=DuckhuntService(db),
        chats=ChatRepo(db),
        users=UserRepo(db),
        command_log=CommandLogRepo(db),
        pgvector_available=pgvector_available,
    )


def build_dispatcher(rt: Runtime) -> Dispatcher:
    dp = Dispatcher()
    # Order matters: command/admin routers first, then duckhunt action triggers,
    # then the catch-all chat handler.
    dp.include_router(basics_h.build_router(rt))
    dp.include_router(admin_h.build_router(rt))
    dp.include_router(debug_h.build_router(rt))
    dp.include_router(ai_h.build_router(rt))
    dp.include_router(duck_h.build_router(rt))
    dp.include_router(chat_h.build_router(rt))
    return dp


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("Starting iPedro V2")
    rt = await build_runtime(settings)
    dp = build_dispatcher(rt)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - windows
            pass

    spawner_task = asyncio.create_task(
        run_spawner(rt.bot, rt.db, rt.openai, settings, stop),
        name="duckhunt-spawner",
    )
    share_photo_task = asyncio.create_task(
        run_share_photo_loop(rt.bot, rt.db, rt.openai, settings, stop),
        name="share-photo",
    )

    try:
        polling = asyncio.create_task(dp.start_polling(rt.bot), name="aiogram-polling")
        # Wait until either polling exits or stop is signaled.
        done, _ = await asyncio.wait(
            {polling, asyncio.create_task(stop.wait(), name="stop-waiter")},
            return_when=asyncio.FIRST_COMPLETED,
        )
        stop.set()
        await dp.stop_polling()
        for t in done:
            if t.exception():
                log.exception("Task exited with error: %s", t.exception())
    finally:
        stop.set()
        for task in (spawner_task, share_photo_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await rt.bot.session.close()
        except Exception:
            pass
        await rt.db.close()
        log.info("Shutdown complete.")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
