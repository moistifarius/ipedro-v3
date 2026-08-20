"""Bot lifecycle: build the aiogram bot/dispatcher, wire routers, run."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from ipedro.config import Settings, get_settings
from ipedro.db.migrations import apply_schema, has_pgvector
from ipedro.db.pool import Database, set_db
from ipedro.db.repositories import ChatRepo, CommandLogRepo, UserRepo
from ipedro.duckhunt.debug_toggles import load_all as load_debug_toggles
from ipedro.duckhunt.service import DuckhuntService
from ipedro.duckhunt.spawner import run_spawner
from ipedro.ambient_loops import run_ambient_loops
from ipedro.handlers import admin as admin_h
from ipedro.handlers import ai as ai_h
from ipedro.handlers import basics as basics_h
from ipedro.handlers import chat as chat_h
from ipedro.handlers import dale as dale_h
from ipedro.handlers import debug as debug_h
from ipedro.handlers import duckhunt as duck_h
from ipedro.handlers import ether as ether_h
from ipedro.handlers import karma as karma_h
from ipedro.handlers import mod as mod_h
from ipedro.handlers import quiz as quiz_h
from ipedro.handlers import utility as utility_h
from ipedro.logging_setup import configure_logging
from ipedro.celebrations import run_celebrations_loop
from ipedro.comic import run_comic_loop
from ipedro.kv import kv_get
from ipedro.personas import set_master_prompt_override
from ipedro.memory.store import MemoryStore
from ipedro.openai_client import OpenAIClient
from ipedro.monthly_recap import run_monthly_recap_loop
from ipedro.persona_state import PersonaStateService
from ipedro.reminders import run_reminders_loop
from ipedro.runtime import Runtime
from ipedro.sharephoto import run_share_photo_loop
from ipedro.silenced_chats import load_all as load_silenced_chats

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

    # Apply persisted overrides for runtime-tunable knobs before constructing
    # the client so the very first request honors them.
    saved_provider = await kv_get(db, "text_provider")
    saved_claude_model = await kv_get(db, "claude_text_model")
    saved_openai_model = await kv_get(db, "openai_text_model")

    text_provider = saved_provider if saved_provider in ("claude", "openai") else settings.text_provider
    if text_provider == "claude" and not settings.anthropic_api_key:
        text_provider = "openai"

    openai = OpenAIClient(
        api_key=settings.openai_api_key,
        organization=settings.openai_organization,
        anthropic_api_key=settings.anthropic_api_key,
        text_provider=text_provider,
        text_model=saved_openai_model or settings.openai_text_model,
        claude_model=saved_claude_model or settings.claude_text_model,
        cheap_claude_model=settings.claude_cheap_model,
        cheap_openai_model=settings.openai_cheap_model,
        image_model=settings.openai_image_model,
        transcription_model=settings.openai_transcription_model,
        embedding_model=settings.openai_embedding_model,
        embedding_dim=settings.openai_embedding_dim,
        tts_model=settings.openai_tts_model,
        tts_voice=settings.openai_tts_voice,
    )
    openai.attach_usage_db(db)
    log.info(
        "AI text provider: %s (claude=%s, openai=%s)",
        openai.text_provider, openai.claude_model, openai.text_model,
    )

    # Pick up any persisted master-prompt override before serving requests.
    # Falls back to the legacy key set by earlier versions.
    override = (
        await kv_get(db, "master_prompt")
        or await kv_get(db, "pedro_master_prompt")
    )
    set_master_prompt_override(override)

    # Prime the admin debug-toggle cache from kv_store so the bot honors
    # any toggles the admin had set before the last restart.
    await load_debug_toggles(db, settings.admin_ids)

    # Prime the admin-only silenced-chats set so ambient loops respect it
    # from the first tick after restart.
    await load_silenced_chats(db)

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
        persona_state=PersonaStateService(db),
        pgvector_available=pgvector_available,
    )


def build_dispatcher(rt: Runtime) -> Dispatcher:
    dp = Dispatcher()

    # Last-resort error handler: an unhandled handler exception otherwise
    # means the user's message silently gets no reply and no trace of why.
    @dp.errors()
    async def on_handler_error(event) -> bool:
        log.exception(
            "Unhandled handler error: %s", event.exception,
            exc_info=event.exception,
        )
        return True

    # Order matters: command/admin routers first, then duckhunt action triggers,
    # then the catch-all chat handler.
    dp.include_router(basics_h.build_router(rt))
    dp.include_router(admin_h.build_router(rt))
    dp.include_router(debug_h.build_router(rt))
    dp.include_router(mod_h.build_router(rt))
    dp.include_router(utility_h.build_router(rt))
    dp.include_router(karma_h.build_router(rt))
    dp.include_router(ai_h.build_router(rt))
    dp.include_router(quiz_h.build_router(rt))
    dp.include_router(duck_h.build_router(rt))
    dp.include_router(ether_h.build_router(rt))
    dp.include_router(dale_h.build_router(rt))
    dp.include_router(chat_h.build_router(rt))
    return dp


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("Starting iDude — the Dude abides.")
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
    reminders_task = asyncio.create_task(
        run_reminders_loop(rt.bot, rt.db, stop),
        name="reminders",
    )
    celebrations_task = asyncio.create_task(
        run_celebrations_loop(rt.bot, rt.db, settings, stop),
        name="celebrations",
    )
    comic_task = asyncio.create_task(
        run_comic_loop(rt.bot, rt.db, rt.openai, stop),
        name="comic",
    )
    ambient_task = asyncio.create_task(
        run_ambient_loops(rt.bot, rt.db, rt.openai, settings, stop),
        name="ambient-loops",
    )
    monthly_recap_task = asyncio.create_task(
        run_monthly_recap_loop(rt.bot, rt.db, rt.openai, settings, stop),
        name="monthly-recap",
    )

    background_tasks = (
        spawner_task, share_photo_task, reminders_task,
        celebrations_task, comic_task, ambient_task,
        monthly_recap_task,
    )

    # A background loop dying is a silently-missing feature until restart —
    # make sure it at least screams in the log.
    def _report_loop_death(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.error("Background loop %r died: %r", t.get_name(), exc)

    for task in background_tasks:
        task.add_done_callback(_report_loop_death)

    stop_waiter: asyncio.Task | None = None
    try:
        polling = asyncio.create_task(
            dp.start_polling(
                rt.bot,
                allowed_updates=dp.resolve_used_update_types(),
            ),
            name="aiogram-polling",
        )
        # Wait until either polling exits or stop is signaled.
        stop_waiter = asyncio.create_task(stop.wait(), name="stop-waiter")
        done, _ = await asyncio.wait(
            {polling, stop_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        stop.set()
        await dp.stop_polling()
        for t in done:
            if not t.cancelled() and t.exception() is not None:
                log.error(
                    "Task %r exited with error: %r", t.get_name(), t.exception(),
                )
    finally:
        stop.set()
        if stop_waiter is not None:
            stop_waiter.cancel()
        for task in background_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.warning(
                    "Task %r cleanup failed", task.get_name(), exc_info=True,
                )
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
