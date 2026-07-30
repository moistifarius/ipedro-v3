"""Interactive disgust personality test (/disgusttest).

A 16-question Likert quiz adapted from validated disgust instruments (see
`ipedro.disgust_test` for the item bank and citations). Each question is one
self-editing message with six tap-buttons; progress is stored durably per
(chat, user) so concurrent takers never collide and double-taps can't
double-count. The result is a generated illustration whose caption carries the
deterministic scores plus a persona-voiced verdict.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

from ipedro import disgust_test as dt
from ipedro.handlers.common import display_name, get_or_create_chat_config
from ipedro.personas import current_master_prompt
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

_VERDICT_INSTRUCTION = (
    "The user just finished a tongue-in-cheek disgust-sensitivity personality "
    "test. In character, give them a short, punchy verdict — at most two "
    "sentences. React to their result (how squeamish they are, their biggest "
    "'ick') instead of reading numbers back like a robot. Plain text, no "
    "markdown, no emoji spam."
)


def _question_text(idx: int) -> str:
    item = dt.ALL_ITEMS[idx]
    section = "Food disgust" if item.section == "food" else "General disgust"
    return (
        f"🧪 <b>Disgust Test</b> · {idx + 1}/{dt.N_ITEMS} · {section}\n\n"
        f"{item.emoji}  How grossed out would you be by…\n\n"
        f"<b>{item.text}?</b>\n\n"
        f"<i>{dt.SCALE_LEGEND}</i>"
    )


def _question_keyboard(owner_id: int, idx: int) -> InlineKeyboardMarkup:
    # callback_data: dq:<owner>:<idx>:<value>  (owner gates who may answer;
    # idx guards against stale/duplicate taps). Cancel is dq:<owner>:x.
    rating = [
        InlineKeyboardButton(text=str(v), callback_data=f"dq:{owner_id}:{idx}:{v}")
        for v in range(dt.SCALE_MIN, dt.SCALE_MAX + 1)
    ]
    cancel = [InlineKeyboardButton(text="✖ cancel", callback_data=f"dq:{owner_id}:x")]
    return InlineKeyboardMarkup(inline_keyboard=[rating, cancel])


def build_router(rt: Runtime) -> Router:
    r = Router(name="quiz")

    @r.message(Command("disgusttest", "icktest", "disgust"))
    async def start_test(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        if not msg.from_user:
            return
        uid = msg.from_user.id
        sent = await msg.answer(
            _question_text(0),
            reply_markup=_question_keyboard(uid, 0),
            parse_mode="HTML",
            disable_notification=True,
        )
        # One session per (chat, user); starting again wipes prior progress.
        await rt.db.execute(
            "INSERT INTO disgust_test_sessions (chat_id, user_id, message_id, "
            "                                   answers, started_at) "
            "VALUES ($1, $2, $3, '{}', NOW()) "
            "ON CONFLICT (chat_id, user_id) DO UPDATE "
            "   SET answers = '{}', message_id = EXCLUDED.message_id, "
            "       started_at = NOW()",
            msg.chat.id, uid, sent.message_id,
        )

    @r.callback_query(F.data.startswith("dq:"))
    async def on_answer(cb: CallbackQuery) -> None:
        if not cb.from_user or cb.message is None:
            await cb.answer()
            return
        parts = cb.data.split(":")
        try:
            owner_id = int(parts[1])
        except (IndexError, ValueError):
            await cb.answer()
            return
        # Only the person who started this test may drive it.
        if cb.from_user.id != owner_id:
            await cb.answer("not your test 🙅", show_alert=False)
            return

        chat_id = cb.message.chat.id

        # Cancel.
        if len(parts) >= 3 and parts[2] == "x":
            await rt.db.execute(
                "DELETE FROM disgust_test_sessions WHERE chat_id=$1 AND user_id=$2",
                chat_id, owner_id,
            )
            await cb.message.edit_text("✖ Disgust test cancelled.")
            await cb.answer("cancelled")
            return

        try:
            idx = int(parts[2])
            value = int(parts[3])
        except (IndexError, ValueError):
            await cb.answer()
            return
        if not (dt.SCALE_MIN <= value <= dt.SCALE_MAX):
            await cb.answer()
            return

        # Atomic, idempotent append: only records the answer if the session is
        # actually waiting on question `idx` (cardinality == idx). A stale or
        # double tap fails the guard and touches nothing.
        row = await rt.db.fetchrow(
            "UPDATE disgust_test_sessions "
            "   SET answers = array_append(answers, $1) "
            " WHERE chat_id = $2 AND user_id = $3 AND cardinality(answers) = $4 "
            "RETURNING answers",
            value, chat_id, owner_id, idx,
        )
        if row is None:
            exists = await rt.db.fetchval(
                "SELECT 1 FROM disgust_test_sessions "
                " WHERE chat_id=$1 AND user_id=$2",
                chat_id, owner_id,
            )
            if exists:
                await cb.answer()  # stale/duplicate tap — quietly ignore
            else:
                await cb.answer(
                    "that test expired — send /disgusttest to start again",
                    show_alert=True,
                )
            return

        answers = list(row["answers"])
        if len(answers) < dt.N_ITEMS:
            await cb.message.edit_text(
                _question_text(len(answers)),
                reply_markup=_question_keyboard(owner_id, len(answers)),
                parse_mode="HTML",
            )
            await cb.answer(f"{value} · {dt.SCALE_WORDS[value]}")
            return

        # Completed — finalize.
        await cb.answer("crunching the numbers 🔬")
        await _finalize(rt, cb, owner_id, answers)

    @r.message(Command("disgustboard", "ickboard"))
    async def leaderboard(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        rows = await rt.db.fetch(
            "SELECT display_name, food_score, general_score, overall_score "
            "  FROM disgust_test_results "
            " WHERE chat_id = $1 ORDER BY overall_score DESC, taken_at ASC "
            " LIMIT 15",
            msg.chat.id,
        )
        if not rows:
            await msg.reply(
                "Nobody's taken the disgust test yet. Be the first: /disgusttest",
                disable_notification=True,
            )
            return
        # Plain text (no parse_mode): display names are user-controlled and
        # would break HTML parsing if they contained < > &.
        medals = {0: "🥇", 1: "🥈", 2: "🥉"}
        lines = ["🧫 Most easily disgusted 🧫\n"]
        for i, row in enumerate(rows):
            tag = medals.get(i, f"{i + 1}.")
            lines.append(
                f"{tag} {row['display_name']} — {row['overall_score']}/6 "
                f"({dt.band(row['overall_score'])})"
            )
        await msg.reply("\n".join(lines), disable_notification=True)

    return r


async def _finalize(
    rt: Runtime, cb: CallbackQuery, owner_id: int, answers: list[int],
) -> None:
    chat_id = cb.message.chat.id
    name = display_name(cb.from_user)
    try:
        result = dt.score(answers)
    except ValueError:
        # Corrupt session — reset so the user can cleanly retry.
        await rt.db.execute(
            "DELETE FROM disgust_test_sessions WHERE chat_id=$1 AND user_id=$2",
            chat_id, owner_id,
        )
        await cb.message.edit_text(
            "Something went wrong scoring that. Send /disgusttest to try again."
        )
        return

    # Feedback while the (slow) image generates.
    await cb.message.edit_text(f"🔬 Crunching {name}'s results…")

    verdict = await _persona_verdict(rt, chat_id, result, name)
    caption = dt.result_caption(result, name, verdict)

    # Persist the result (latest per user) and clear the session.
    await rt.db.execute(
        "INSERT INTO disgust_test_results (chat_id, user_id, display_name, "
        "  food_score, general_score, core_score, animal_score, contam_score, "
        "  overall_score, biggest_ick, taken_at) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, NOW()) "
        "ON CONFLICT (chat_id, user_id) DO UPDATE SET "
        "  display_name=EXCLUDED.display_name, food_score=EXCLUDED.food_score, "
        "  general_score=EXCLUDED.general_score, core_score=EXCLUDED.core_score, "
        "  animal_score=EXCLUDED.animal_score, contam_score=EXCLUDED.contam_score, "
        "  overall_score=EXCLUDED.overall_score, biggest_ick=EXCLUDED.biggest_ick, "
        "  taken_at=NOW()",
        chat_id, owner_id, name,
        result.food_score, result.general_score, result.core_score,
        result.animal_score, result.contam_score, result.overall_score,
        result.biggest_ick_label,
    )
    await rt.db.execute(
        "DELETE FROM disgust_test_sessions WHERE chat_id=$1 AND user_id=$2",
        chat_id, owner_id,
    )

    image = None
    try:
        image = await rt.openai.generate_image(dt.image_prompt(result))
    except Exception as exc:  # image gen is best-effort flavour
        log.warning("disgust result image failed: %s", exc)

    if image:
        await cb.message.answer_photo(
            BufferedInputFile(image, filename="disgust.png"),
            caption=caption,
            disable_notification=True,
        )
        await cb.message.edit_text(f"🧫 {name}'s disgust profile 👇")
    else:
        # No image — the caption carries everything, so show it as the result.
        await cb.message.edit_text(caption)


async def _persona_verdict(
    rt: Runtime, chat_id: int, result: dt.DisgustResult, name: str,
) -> str:
    payload = (
        f"Name: {name}. Overall: {result.overall_band} "
        f"({result.overall_score}/6). Food disgust {result.food_score}/6 "
        f"({result.food_band}). General disgust {result.general_score}/6 "
        f"({result.general_band}). Biggest ick: {result.biggest_ick_label}. "
        f"Iron stomach: {result.iron_stomach_label}."
    )
    try:
        ai = await rt.openai.chat(
            [
                {"role": "system", "content": current_master_prompt()},
                {"role": "system", "content": _VERDICT_INSTRUCTION},
                {"role": "user", "content": payload},
            ],
            max_tokens=160,
            chat_id=chat_id,
        )
        if ai and ai.strip():
            return ai.strip()
    except Exception as exc:
        log.warning("disgust verdict generation failed: %s", exc)
    return dt.fallback_verdict(result, name)
