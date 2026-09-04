"""Router for the personality-quiz engine.

Thin wiring only — the flow, scoring, images and storage live in
`ipedro.quizzes.engine`, and each quiz is a data definition in
`ipedro.quizzes`. This registers the shared callbacks, the /tests menu, the
admin warm-up, and every quiz's own start + leaderboard commands.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ipedro.quizzes import engine, registry
from ipedro.runtime import Runtime


def _make_start(rt: Runtime, quiz):
    async def _start(msg: Message) -> None:
        await engine.start_command(rt, quiz, msg)
    _start.__name__ = f"start_{quiz.id}"
    return _start


def _make_board(rt: Runtime, quiz):
    async def _board(msg: Message) -> None:
        await engine.leaderboard(rt, quiz, msg)
    _board.__name__ = f"board_{quiz.id}"
    return _board


def build_router(rt: Runtime) -> Router:
    r = Router(name="quiz")

    @r.message(Command("tests", "quiz", "quizzes"))
    async def tests_menu(msg: Message) -> None:
        await engine.show_menu(rt, msg)

    @r.message(Command("quiz_warmup", "disgust_warmup"))
    async def quiz_warmup(msg: Message) -> None:
        await engine.warmup_command(rt, msg)

    @r.callback_query(F.data.startswith("qgo:"))
    async def on_go(cb: CallbackQuery) -> None:
        await engine.on_go(rt, cb)

    @r.callback_query(F.data.startswith("qr:"))
    async def on_retake(cb: CallbackQuery) -> None:
        await engine.on_retake(rt, cb)

    @r.callback_query(F.data.startswith("q:"))
    async def on_answer(cb: CallbackQuery) -> None:
        await engine.on_answer(rt, cb)

    # Each quiz's own start + (optional) leaderboard commands.
    for quiz in registry.all_quizzes():
        r.message.register(_make_start(rt, quiz), Command(*quiz.commands))
        if quiz.board_commands:
            r.message.register(_make_board(rt, quiz), Command(*quiz.board_commands))

    return r
