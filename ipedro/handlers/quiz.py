"""Interactive disgust personality test (/disgusttest).

A 16-question Likert quiz adapted from validated disgust instruments (see
`ipedro.disgust_test` for the item bank and citations). Progress is stored
durably per (chat, user) so concurrent takers never collide and double-taps
can't double-count.

Each of the 16 items has a fixed cartoon illustration, generated once via the
image model and cached in `disgust_item_images` (shared across every chat/user).
When all 16 are cached the quiz runs as a single evolving *photo* message
(`edit_media` per answer); until then it falls back to an emoji text flow and
kicks a background warm-up. The result adds bar-chart meters, a chat percentile,
a persona-voiced verdict, a generated result card, and a Retake button.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, Message,
)

from ipedro import disgust_test as dt
from ipedro.handlers.common import (
    display_name, get_or_create_chat_config, require_admin,
)
from ipedro.personas import current_master_prompt
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

_ITEM_KEYS = [it.key for it in dt.ALL_ITEMS]

_VERDICT_INSTRUCTION = (
    "The user just finished a tongue-in-cheek disgust-sensitivity personality "
    "test. In character, give them a short, punchy verdict — at most two "
    "sentences. React to their result (how squeamish they are, their biggest "
    "'ick') instead of reading numbers back like a robot. Plain text, no "
    "markdown, no emoji spam."
)

# Only one background image warm-up runs at a time, process-wide.
_warmup_lock = asyncio.Lock()


# --------------------------------------------------------------- rendering
def _question_caption(idx: int) -> str:
    item = dt.ALL_ITEMS[idx]
    section = "Food disgust" if item.section == "food" else "General disgust"
    bar = dt.progress_bar(idx + 1, dt.N_ITEMS)
    return (
        f"🧪 <b>Disgust Test</b>  {bar}  {idx + 1}/{dt.N_ITEMS}\n"
        f"<i>{section}</i>\n\n"
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


def _retake_keyboard(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Retake", callback_data=f"dqx:{owner_id}"),
    ]])


async def _set_message(
    cb: CallbackQuery, text: str, *, reply_markup=None, parse_mode: str | None = "HTML",
) -> None:
    """Edit the flow message's text/caption, whichever it has. A photo message
    can't be edited with edit_text (and vice versa), so pick by type."""
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
async def _all_images_cached(rt: Runtime) -> bool:
    n = await rt.db.fetchval(
        "SELECT COUNT(*) FROM disgust_item_images WHERE item_key = ANY($1::text[])",
        _ITEM_KEYS,
    )
    return int(n or 0) >= dt.N_ITEMS


async def _get_item_image(rt: Runtime, key: str) -> bytes | None:
    png = await rt.db.fetchval(
        "SELECT png FROM disgust_item_images WHERE item_key = $1", key,
    )
    return bytes(png) if png is not None else None


def _kick_warmup(rt: Runtime) -> None:
    """Fire-and-forget the image warm-up, if one isn't already running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # no loop (shouldn't happen in the bot) — skip
        return
    if _warmup_lock.locked():
        return
    loop.create_task(_warm_item_images(rt))


async def _warm_item_images(rt: Runtime) -> None:
    """Generate and cache any missing per-item illustrations. Best-effort:
    each failure is logged and skipped so the next run retries it."""
    if _warmup_lock.locked():
        return
    async with _warmup_lock:
        for item in dt.ALL_ITEMS:
            if await rt.db.fetchval(
                "SELECT 1 FROM disgust_item_images WHERE item_key = $1", item.key,
            ):
                continue
            try:
                png = await rt.openai.generate_image(dt.item_image_prompt(item))
            except Exception as exc:
                log.warning("disgust warmup: image failed for %s: %s", item.key, exc)
                continue
            if png:
                await rt.db.execute(
                    "INSERT INTO disgust_item_images (item_key, png) "
                    "VALUES ($1, $2) ON CONFLICT (item_key) DO NOTHING",
                    item.key, png,
                )
    log.info("disgust warmup pass complete")


# --------------------------------------------------------------- flow
async def _begin(rt: Runtime, target, chat_id: int, uid: int) -> None:
    """Send question 1 and (re)create the session. `target` exposes .answer /
    .answer_photo (a Message, or the message under a callback for retakes)."""
    img = await _get_item_image(rt, dt.ALL_ITEMS[0].key) \
        if await _all_images_cached(rt) else None
    if img:
        sent = await target.answer_photo(
            BufferedInputFile(img, filename="q1.png"),
            caption=_question_caption(0),
            reply_markup=_question_keyboard(uid, 0),
            parse_mode="HTML", disable_notification=True,
        )
    else:
        sent = await target.answer(
            _question_caption(0),
            reply_markup=_question_keyboard(uid, 0),
            parse_mode="HTML", disable_notification=True,
        )
        _kick_warmup(rt)   # populate the cache so the next run is picture-mode
    await rt.db.execute(
        "INSERT INTO disgust_test_sessions (chat_id, user_id, message_id, "
        "                                   answers, started_at) "
        "VALUES ($1, $2, $3, '{}', NOW()) "
        "ON CONFLICT (chat_id, user_id) DO UPDATE "
        "   SET answers = '{}', message_id = EXCLUDED.message_id, "
        "       started_at = NOW()",
        chat_id, uid, sent.message_id,
    )


async def _render_next_question(
    rt: Runtime, cb: CallbackQuery, owner_id: int, next_idx: int,
) -> None:
    caption = _question_caption(next_idx)
    kb = _question_keyboard(owner_id, next_idx)
    if getattr(cb.message, "photo", None):
        img = await _get_item_image(rt, dt.ALL_ITEMS[next_idx].key)
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
        # No image this step — keep the current picture, refresh the caption.
        await _set_message(cb, caption, reply_markup=kb)
    else:
        await _set_message(cb, caption, reply_markup=kb)


def build_router(rt: Runtime) -> Router:
    r = Router(name="quiz")

    @r.message(Command("disgusttest", "icktest", "disgust"))
    async def start_test(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        if not msg.from_user:
            return
        await _begin(rt, msg, msg.chat.id, msg.from_user.id)

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
            await _set_message(cb, "✖ Disgust test cancelled.",
                               reply_markup=None, parse_mode=None)
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
            await _render_next_question(rt, cb, owner_id, len(answers))
            await cb.answer(f"{value} · {dt.SCALE_WORDS[value]}")
            return

        # Completed — finalize.
        await cb.answer("crunching the numbers 🔬")
        await _finalize(rt, cb, owner_id, answers)

    @r.callback_query(F.data.startswith("dqx:"))
    async def on_retake(cb: CallbackQuery) -> None:
        if not cb.from_user or cb.message is None:
            await cb.answer()
            return
        try:
            owner_id = int(cb.data.split(":")[1])
        except (IndexError, ValueError):
            await cb.answer()
            return
        if cb.from_user.id != owner_id:
            await cb.answer("not your test 🙅", show_alert=False)
            return
        await cb.answer("restarting 🔁")
        await _begin(rt, cb.message, cb.message.chat.id, owner_id)

    @r.message(Command("disgust_warmup"))
    async def disgust_warmup(msg: Message) -> None:
        if not await require_admin(msg, rt.settings.admin_ids):
            return
        n = await rt.db.fetchval(
            "SELECT COUNT(*) FROM disgust_item_images "
            " WHERE item_key = ANY($1::text[])",
            _ITEM_KEYS,
        )
        _kick_warmup(rt)
        await msg.reply(
            f"🖼 Disgust images: {int(n or 0)}/{dt.N_ITEMS} cached. "
            "Generating any missing ones in the background — re-run to check.",
            disable_notification=True,
        )

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


async def _chat_percentile(
    rt: Runtime, chat_id: int, user_id: int, score: float,
) -> int | None:
    """Share of OTHER takers in this chat scoring lower than `score`. None when
    nobody else has taken it (nothing to compare against)."""
    row = await rt.db.fetchrow(
        "SELECT COUNT(*) AS total, "
        "       COUNT(*) FILTER (WHERE overall_score < $2) AS below "
        "  FROM disgust_test_results "
        " WHERE chat_id = $1 AND user_id <> $3",
        chat_id, score, user_id,
    )
    if not row or not row["total"]:
        return None
    return round(row["below"] / row["total"] * 100)


async def _finalize(
    rt: Runtime, cb: CallbackQuery, owner_id: int, answers: list[int],
) -> None:
    chat_id = cb.message.chat.id
    name = display_name(cb.from_user)
    try:
        result = dt.score(answers)
    except ValueError:
        await rt.db.execute(
            "DELETE FROM disgust_test_sessions WHERE chat_id=$1 AND user_id=$2",
            chat_id, owner_id,
        )
        await _set_message(
            cb, "Something went wrong scoring that. Send /disgusttest to try again.",
            parse_mode=None,
        )
        return

    # Feedback while the (slow) result image generates.
    await _set_message(cb, f"🔬 Crunching {name}'s results…", parse_mode=None)

    verdict = await _persona_verdict(rt, chat_id, result, name)
    percentile = await _chat_percentile(rt, chat_id, owner_id, result.overall_score)
    caption = dt.result_caption(result, name, verdict, percentile=percentile)

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

    kb = _retake_keyboard(owner_id)
    if getattr(cb.message, "photo", None):
        # The flow message is already a photo — swap in the result card.
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
            await _set_message(cb, f"🧫 {name}'s disgust profile 👇", parse_mode=None)
        else:
            # No image — the caption carries everything, so show it as the result.
            await _set_message(cb, caption, reply_markup=kb, parse_mode=None)


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
