"""The quiz engine: everything a quiz doesn't have to know about.

Drives the interactive flow (one evolving photo/text message with tap-buttons),
caches a generated illustration per item, scores on completion, renders a
uniform result card with ▰▱ meters + a chat percentile + a persona verdict,
stores the result, and serves the per-quiz leaderboard and the /tests menu.

All state is keyed by (quiz_id, chat_id, user_id): concurrent takers and
different quizzes never collide, and the answer append is an atomic
cardinality-guarded UPDATE so double/stale taps can't double-count.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, Message,
)

from ipedro.handlers.common import (
    display_name, get_or_create_chat_config, require_admin,
)
from ipedro.personas import current_master_prompt
from ipedro.quizzes import image_fetch, registry
from ipedro.quizzes.types import Quiz, QuizResult
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

# Only one background image warm-up runs at a time, process-wide.
_warmup_lock = asyncio.Lock()


# --------------------------------------------------------------- rendering
def progress_bar(position: int, total: int, width: int = 10) -> str:
    filled = max(0, min(width, round(position / total * width)))
    return "▰" * filled + "▱" * (width - filled)


def meter(fraction: float, width: int = 10) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "▰" * filled + "▱" * (width - filled)


def question_caption(quiz: Quiz, idx: int) -> str:
    item = quiz.items[idx]
    bar = progress_bar(idx + 1, quiz.n_items)
    section = f"\n<i>{item.section}</i>" if item.section else ""
    return (
        f"{quiz.emoji} <b>{quiz.title}</b>  {bar}  {idx + 1}/{quiz.n_items}{section}\n\n"
        f"{item.emoji}  {quiz.prompt_lead}\n\n"
        f"<b>{item.text}</b>\n\n"
        f"<i>{quiz.scale_legend}</i>"
    )


def _rating_keyboard(quiz: Quiz, owner_id: int, idx: int) -> InlineKeyboardMarkup:
    rating = [
        InlineKeyboardButton(
            text=str(v), callback_data=f"q:{quiz.id}:{owner_id}:{idx}:{v}",
        )
        for v in range(quiz.scale_min, quiz.scale_max + 1)
    ]
    cancel = [InlineKeyboardButton(
        text="✖ cancel", callback_data=f"q:{quiz.id}:{owner_id}:x")]
    return InlineKeyboardMarkup(inline_keyboard=[rating, cancel])


def _retake_keyboard(quiz: Quiz, owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Retake", callback_data=f"qr:{quiz.id}:{owner_id}"),
    ]])


def result_caption(
    quiz: Quiz, result: QuizResult, name: str, verdict: str,
    *, percentile: int | None = None,
) -> str:
    lines = [f"{quiz.emoji} {name}'s {quiz.title}", f"— {result.summary} —", ""]
    for label, frac, vtext in result.meters:
        lines.append(f"{label} {meter(frac)} {vtext}")
    lines += list(result.extras)
    if percentile is not None:
        lines.append(f"🏅 Ranks above {percentile}% of this chat")
    lines += ["", verdict.strip(), "", f"📚 {quiz.citation}"]
    caption = "\n".join(lines)
    if len(caption) > 1024:
        overflow = len(caption) - 1024 + 1
        trimmed = verdict.strip()[: max(0, len(verdict.strip()) - overflow - 1)] + "…"
        try:
            lines[lines.index(verdict.strip())] = trimmed
        except ValueError:
            pass
        caption = "\n".join(lines)
    return caption[:1024]


async def _set_message(
    cb: CallbackQuery, text: str, *, reply_markup=None, parse_mode: str | None = "HTML",
) -> None:
    """Edit the flow message's text or caption, whichever it has."""
    m = cb.message
    try:
        if getattr(m, "photo", None):
            await m.edit_caption(caption=text, reply_markup=reply_markup,
                                 parse_mode=parse_mode)
        else:
            await m.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest:
        pass


# --------------------------------------------------------------- image cache
async def _all_images_cached(rt: Runtime, quiz: Quiz) -> bool:
    keys = [it.key for it in quiz.items]
    n = await rt.db.fetchval(
        "SELECT COUNT(*) FROM quiz_item_images "
        " WHERE quiz_id = $1 AND item_key = ANY($2::text[])",
        quiz.id, keys,
    )
    return int(n or 0) >= quiz.n_items


async def _get_item_image(rt: Runtime, quiz: Quiz, key: str) -> bytes | None:
    png = await rt.db.fetchval(
        "SELECT png FROM quiz_item_images WHERE quiz_id = $1 AND item_key = $2",
        quiz.id, key,
    )
    return bytes(png) if png is not None else None


def _kick_warmup(rt: Runtime, quiz: Quiz) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _warmup_lock.locked():
        return
    loop.create_task(_warm_item_images(rt, quiz))


async def _warm_item_images(rt: Runtime, quiz: Quiz) -> None:
    """Generate + cache any missing illustrations for this quiz. Best-effort."""
    if _warmup_lock.locked():
        return
    async with _warmup_lock:
        for item in quiz.items:
            if await rt.db.fetchval(
                "SELECT 1 FROM quiz_item_images "
                " WHERE quiz_id = $1 AND item_key = $2", quiz.id, item.key,
            ):
                continue
            png = await image_fetch.fetch(item.image_query or item.text)
            if png:
                await rt.db.execute(
                    "INSERT INTO quiz_item_images (quiz_id, item_key, png) "
                    "VALUES ($1, $2, $3) ON CONFLICT (quiz_id, item_key) DO NOTHING",
                    quiz.id, item.key, png,
                )
    log.info("quiz warmup pass complete for %s", quiz.id)


async def images_cached_count(rt: Runtime, quiz: Quiz) -> int:
    keys = [it.key for it in quiz.items]
    n = await rt.db.fetchval(
        "SELECT COUNT(*) FROM quiz_item_images "
        " WHERE quiz_id = $1 AND item_key = ANY($2::text[])",
        quiz.id, keys,
    )
    return int(n or 0)


# --------------------------------------------------------------- flow
async def begin(rt: Runtime, quiz: Quiz, target, chat_id: int, uid: int) -> None:
    """Send question 1 (photo if all images cached, else text) and (re)create
    the session. `target` exposes .answer / .answer_photo."""
    img = await _get_item_image(rt, quiz, quiz.items[0].key) \
        if await _all_images_cached(rt, quiz) else None
    if img:
        sent = await target.answer_photo(
            BufferedInputFile(img, filename="q1.png"),
            caption=question_caption(quiz, 0),
            reply_markup=_rating_keyboard(quiz, uid, 0),
            parse_mode="HTML", disable_notification=True,
        )
    else:
        sent = await target.answer(
            question_caption(quiz, 0),
            reply_markup=_rating_keyboard(quiz, uid, 0),
            parse_mode="HTML", disable_notification=True,
        )
        _kick_warmup(rt, quiz)
    await rt.db.execute(
        "INSERT INTO quiz_sessions (quiz_id, chat_id, user_id, message_id, "
        "                           answers, started_at) "
        "VALUES ($1, $2, $3, $4, '{}', NOW()) "
        "ON CONFLICT (quiz_id, chat_id, user_id) DO UPDATE "
        "   SET answers = '{}', message_id = EXCLUDED.message_id, started_at = NOW()",
        quiz.id, chat_id, uid, sent.message_id,
    )


async def _render_next_question(
    rt: Runtime, quiz: Quiz, cb: CallbackQuery, owner_id: int, next_idx: int,
) -> None:
    caption = question_caption(quiz, next_idx)
    kb = _rating_keyboard(quiz, owner_id, next_idx)
    if getattr(cb.message, "photo", None):
        img = await _get_item_image(rt, quiz, quiz.items[next_idx].key)
        if img:
            try:
                await cb.message.edit_media(
                    InputMediaPhoto(
                        media=BufferedInputFile(img, filename=f"q{next_idx + 1}.png"),
                        caption=caption, parse_mode="HTML",
                    ),
                    reply_markup=kb,
                )
                return
            except TelegramBadRequest:
                pass
        await _set_message(cb, caption, reply_markup=kb)
    else:
        await _set_message(cb, caption, reply_markup=kb)


async def _chat_percentile(
    rt: Runtime, quiz: Quiz, chat_id: int, user_id: int, score: float,
) -> int | None:
    row = await rt.db.fetchrow(
        "SELECT COUNT(*) AS total, "
        "       COUNT(*) FILTER (WHERE headline_score < $3) AS below "
        "  FROM quiz_results "
        " WHERE quiz_id = $1 AND chat_id = $2 AND user_id <> $4",
        quiz.id, chat_id, score, user_id,
    )
    if not row or not row["total"]:
        return None
    return round(row["below"] / row["total"] * 100)


async def _finalize(
    rt: Runtime, quiz: Quiz, cb: CallbackQuery, owner_id: int, answers: list[int],
) -> None:
    chat_id = cb.message.chat.id
    name = display_name(cb.from_user)
    try:
        result = quiz.score(answers)
    except ValueError:
        await rt.db.execute(
            "DELETE FROM quiz_sessions "
            " WHERE quiz_id=$1 AND chat_id=$2 AND user_id=$3",
            quiz.id, chat_id, owner_id,
        )
        await _set_message(
            cb, f"Something went wrong scoring that. Send /{quiz.commands[0]} "
                "to try again.", parse_mode=None,
        )
        return

    await _set_message(cb, f"🔬 Crunching {name}'s results…", parse_mode=None)

    verdict = await _persona_verdict(rt, quiz, chat_id, result, name)
    # Only ranked quizzes (those with a leaderboard) show a chat percentile;
    # a multi-axis profile like Big Five has no single "higher" to compare.
    percentile = (
        await _chat_percentile(rt, quiz, chat_id, owner_id, result.headline)
        if quiz.board_commands else None
    )
    caption = result_caption(quiz, result, name, verdict, percentile=percentile)

    await rt.db.execute(
        "INSERT INTO quiz_results (quiz_id, chat_id, user_id, display_name, "
        "  headline_score, summary, detail, taken_at) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb, NOW()) "
        "ON CONFLICT (quiz_id, chat_id, user_id) DO UPDATE SET "
        "  display_name=EXCLUDED.display_name, headline_score=EXCLUDED.headline_score, "
        "  summary=EXCLUDED.summary, detail=EXCLUDED.detail, taken_at=NOW()",
        quiz.id, chat_id, owner_id, name,
        result.headline, result.summary, json.dumps(result.detail),
    )
    await rt.db.execute(
        "DELETE FROM quiz_sessions WHERE quiz_id=$1 AND chat_id=$2 AND user_id=$3",
        quiz.id, chat_id, owner_id,
    )

    image = await image_fetch.fetch(result.image_subject)

    kb = _retake_keyboard(quiz, owner_id)
    if getattr(cb.message, "photo", None):
        if image:
            try:
                await cb.message.edit_media(
                    InputMediaPhoto(
                        media=BufferedInputFile(image, filename="result.png"),
                        caption=caption,
                    ),
                    reply_markup=kb,
                )
                return
            except TelegramBadRequest:
                pass
        await _set_message(cb, caption, reply_markup=kb, parse_mode=None)
    else:
        if image:
            await cb.message.answer_photo(
                BufferedInputFile(image, filename="result.png"),
                caption=caption, reply_markup=kb, disable_notification=True,
            )
            await _set_message(cb, f"{quiz.emoji} {name}'s result 👇", parse_mode=None)
        else:
            await _set_message(cb, caption, reply_markup=kb, parse_mode=None)


async def _persona_verdict(
    rt: Runtime, quiz: Quiz, chat_id: int, result: QuizResult, name: str,
) -> str:
    try:
        ai = await rt.openai.chat(
            [
                {"role": "system", "content": current_master_prompt()},
                {"role": "system", "content": quiz.verdict_instruction},
                {"role": "user", "content": f"Name: {name}. {result.verdict_payload}"},
            ],
            max_tokens=160, chat_id=chat_id,
        )
        if ai and ai.strip():
            return ai.strip()
    except Exception as exc:
        log.warning("quiz verdict generation failed (%s): %s", quiz.id, exc)
    return f"{name}: {result.summary}. Make of that what you will."


# --------------------------------------------------------------- callbacks
async def on_go(rt: Runtime, cb: CallbackQuery) -> None:
    """A quiz was chosen from the /tests menu."""
    if not cb.from_user or cb.message is None:
        await cb.answer()
        return
    quiz = registry.get(cb.data.split(":", 1)[1] if ":" in cb.data else "")
    if quiz is None:
        await cb.answer()
        return
    await cb.answer(f"starting {quiz.title}")
    await begin(rt, quiz, cb.message, cb.message.chat.id, cb.from_user.id)


async def on_retake(rt: Runtime, cb: CallbackQuery) -> None:
    if not cb.from_user or cb.message is None:
        await cb.answer()
        return
    parts = cb.data.split(":")           # qr:<quiz>:<owner>
    quiz = registry.get(parts[1]) if len(parts) > 1 else None
    if quiz is None:
        await cb.answer()
        return
    try:
        owner_id = int(parts[2])
    except (IndexError, ValueError):
        await cb.answer()
        return
    if cb.from_user.id != owner_id:
        await cb.answer("not your test 🙅")
        return
    await cb.answer("restarting 🔁")
    await begin(rt, quiz, cb.message, cb.message.chat.id, owner_id)


async def on_answer(rt: Runtime, cb: CallbackQuery) -> None:
    if not cb.from_user or cb.message is None:
        await cb.answer()
        return
    parts = cb.data.split(":")           # q:<quiz>:<owner>:<idx>:<val> | q:<quiz>:<owner>:x
    quiz = registry.get(parts[1]) if len(parts) > 2 else None
    if quiz is None:
        await cb.answer()
        return
    try:
        owner_id = int(parts[2])
    except (IndexError, ValueError):
        await cb.answer()
        return
    if cb.from_user.id != owner_id:
        await cb.answer("not your test 🙅", show_alert=False)
        return

    chat_id = cb.message.chat.id

    if len(parts) >= 4 and parts[3] == "x":
        await rt.db.execute(
            "DELETE FROM quiz_sessions WHERE quiz_id=$1 AND chat_id=$2 AND user_id=$3",
            quiz.id, chat_id, owner_id,
        )
        await _set_message(cb, f"✖ {quiz.title} cancelled.",
                           reply_markup=None, parse_mode=None)
        await cb.answer("cancelled")
        return

    try:
        idx = int(parts[3])
        value = int(parts[4])
    except (IndexError, ValueError):
        await cb.answer()
        return
    if not (quiz.scale_min <= value <= quiz.scale_max):
        await cb.answer()
        return

    row = await rt.db.fetchrow(
        "UPDATE quiz_sessions SET answers = array_append(answers, $1) "
        " WHERE quiz_id=$2 AND chat_id=$3 AND user_id=$4 AND cardinality(answers)=$5 "
        "RETURNING answers",
        value, quiz.id, chat_id, owner_id, idx,
    )
    if row is None:
        exists = await rt.db.fetchval(
            "SELECT 1 FROM quiz_sessions WHERE quiz_id=$1 AND chat_id=$2 AND user_id=$3",
            quiz.id, chat_id, owner_id,
        )
        if exists:
            await cb.answer()
        else:
            await cb.answer(
                f"that test expired — send /{quiz.commands[0]} to start again",
                show_alert=True,
            )
        return

    answers = list(row["answers"])
    if len(answers) < quiz.n_items:
        await _render_next_question(rt, quiz, cb, owner_id, len(answers))
        await cb.answer(f"{value} · {quiz.scale_words.get(value, str(value))}")
        return

    await cb.answer("crunching the numbers 🔬")
    await _finalize(rt, quiz, cb, owner_id, answers)


# --------------------------------------------------------------- commands
async def start_command(rt: Runtime, quiz: Quiz, msg: Message) -> None:
    await get_or_create_chat_config(rt, msg)
    if not msg.from_user:
        return
    await begin(rt, quiz, msg, msg.chat.id, msg.from_user.id)


async def leaderboard(rt: Runtime, quiz: Quiz, msg: Message) -> None:
    await get_or_create_chat_config(rt, msg)
    rows = await rt.db.fetch(
        "SELECT display_name, headline_score, summary FROM quiz_results "
        " WHERE quiz_id = $1 AND chat_id = $2 "
        " ORDER BY headline_score DESC, taken_at ASC LIMIT 15",
        quiz.id, msg.chat.id,
    )
    if not rows:
        await msg.reply(
            f"Nobody's taken {quiz.title} yet. Be the first: /{quiz.commands[0]}",
            disable_notification=True,
        )
        return
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    lines = [f"{quiz.emoji} {quiz.title} — leaderboard\n"]
    for i, row in enumerate(rows):
        tag = medals.get(i, f"{i + 1}.")
        lines.append(f"{tag} {row['display_name']} — {row['summary']}")
    await msg.reply("\n".join(lines), disable_notification=True)


async def show_menu(rt: Runtime, msg: Message) -> None:
    await get_or_create_chat_config(rt, msg)
    quizzes = registry.all_quizzes()
    rows = [[InlineKeyboardButton(text=f"{q.emoji} {q.title}",
                                  callback_data=f"qgo:{q.id}")] for q in quizzes]
    lines = ["🧠 <b>Personality tests</b>",
             "Tap one — answer the questions, get a scored profile.\n"]
    for q in quizzes:
        lines.append(f"{q.emoji} <b>{q.title}</b> — {q.blurb}")
    await msg.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML", disable_notification=True,
    )


async def warmup_command(rt: Runtime, msg: Message) -> None:
    """Admin: pre-generate every quiz's illustrations."""
    if not await require_admin(msg, rt.settings.admin_ids):
        return
    parts = []
    for quiz in registry.all_quizzes():
        n = await images_cached_count(rt, quiz)
        parts.append(f"{quiz.emoji} {quiz.title}: {n}/{quiz.n_items}")
        _kick_warmup(rt, quiz)
    await msg.reply(
        "🖼 Cached illustrations:\n" + "\n".join(parts) +
        "\n\nFetching any missing from the web in the background — re-run to check.",
        disable_notification=True,
    )
