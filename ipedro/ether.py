"""Ether: pager-style cross-chat message garbling.

Every ~hour, with low probability, picks a recent message from one
opted-in chat, runs it through a pager-style garble (dropped chars,
substitutions, ALL CAPS, blackouts, truncation), and broadcasts it
into a *different* opted-in chat with a spooky wrapper.

The chats are linked only by their consent — neither side knows who's
on the other end. From the receiver's POV it's a stray transmission
they happen to be picking up out of the ether.

Opt-in: ``chat_config.ether_enabled`` (default off).

Cooldown: receiving chats won't get a transmission more than once per
4 hours (``chat_state.last_ether_at``). Sending isn't rate-limited
beyond the loop tick × dice roll — a chatty source chat is fine since
each broadcast goes to a randomly-chosen receiver anyway.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import BufferedInputFile

from ipedro.bot_messages import track
from ipedro.db.pool import Database
from ipedro.radio_fx import apply_radio_effect
from ipedro.silenced_chats import is_silenced

log = logging.getLogger(__name__)

_TICK_SECONDS = 3600          # 1h
_DROP_CHANCE = 0.30           # per tick → roughly one broadcast every ~3h
_RECEIVER_COOLDOWN = timedelta(hours=4)
_SOURCE_LOOKBACK = timedelta(hours=24)
_MIN_MESSAGE_LEN = 25         # don't pick "k", "lol", etc.
_MAX_MESSAGE_LEN = 240        # pager-style: alphanumeric pagers held ~240 chars

_WRAPPERS = (
    "📟 *beep* *beep* incoming\n\n{body}",
    "📟 [transmission from the ether]\n\n{body}",
    "📟 ▓▓▓ signal acquired ▓▓▓\n\n{body}",
    "📟 ...catching a stray page...\n\n{body}",
    "📟 MSG-{code}\n{body}",
)

# Captions used on the voice-note variant (no body text — the audio IS
# the message).
_VOICE_CAPTIONS = (
    "📻 ...incoming transmission...",
    "📻 [stray signal in the static]",
    "📻 ▓▓▓ ...do you copy... ▓▓▓",
    "📻 something on the airwaves",
    "📻 *crackle* ...is anyone receiving...",
)

_SUBS = {
    "o": "0", "i": "1", "e": "3", "s": "5", "a": "@",
    "l": "1", "t": "7", "b": "8", "g": "9",
}


def garble_pager(
    text: str,
    *,
    intensity: float = 0.5,
    rng: random.Random | None = None,
) -> str:
    """Pure-python pager-style garble. Deterministic when ``rng`` is seeded.

    ``intensity`` ∈ [0, 1] scales every probability. At 0 the output is
    still pager-flavored (some caps, the occasional sub) — the signal
    is *always* a little degraded, that's the conceit. At 1 it's
    barely-readable static. Defaults to 0.5 (moderate corruption).

    Char-level: drops and leet substitutions. Word-level: ALL CAPS and
    full-word blackouts. Whole-string: possible trailing truncation.
    Always returns at least ``***`` so callers don't have to handle empty.
    """
    r = rng if rng is not None else random
    text = (text or "").strip()[:_MAX_MESSAGE_LEN]
    if not text:
        return "***"

    i = max(0.0, min(1.0, intensity))
    drop_p     = 0.02 + 0.18 * i      # 0.02 → 0.20
    sub_p      = 0.05 + 0.30 * i      # 0.05 → 0.35  (rolled after drop)
    blackout_p = 0.02 + 0.20 * i      # 0.02 → 0.22
    caps_p     = 0.20 + 0.40 * i      # 0.20 → 0.60
    trunc_p    = 0.10 + 0.50 * i      # 0.10 → 0.60

    out_chars: list[str] = []
    for c in text:
        roll = r.random()
        if roll < drop_p:
            continue
        if roll < drop_p + sub_p and c.isalpha():
            sub = _SUBS.get(c.lower())
            if sub is not None:
                out_chars.append(sub)
                continue
        out_chars.append(c)
    s = "".join(out_chars)

    words = s.split()
    transformed: list[str] = []
    for w in words:
        roll = r.random()
        if roll < blackout_p:
            transformed.append("***")
        elif roll < blackout_p + caps_p:
            transformed.append(w.upper())
        else:
            transformed.append(w)
    s = " ".join(transformed)

    if r.random() < trunc_p and len(s) > 30:
        # More intense = cut earlier on average.
        lo = 20
        hi = max(lo + 5, int(len(s) * (1.0 - 0.6 * i)))
        cut = r.randint(lo, hi)
        s = s[:cut] + "…"
    return s.strip() or "***"


def _roll_intensity(rng: random.Random | None = None) -> float:
    """Per-broadcast intensity. Triangular(0.15, 0.95, mode=0.5) so most
    transmissions are mid-corrupted with occasional pristine-ish and
    occasional total static. Pure python — random.triangular is stdlib."""
    r = rng if rng is not None else random
    return r.triangular(0.15, 0.95, 0.5)


def _roll_radio_intensity(rng: random.Random | None = None) -> float:
    """Intensity for the /ether radio-voice effect. Biased HIGH —
    Triangular(0.55, 1.0, mode=0.85) — so every transmission sounds like
    a genuine long-haul DX signal (heavy fade, drift, static), with the
    occasional one barely punching through at all."""
    r = rng if rng is not None else random
    return r.triangular(0.55, 1.0, 0.85)


def _wrap(body: str, *, rng: random.Random | None = None) -> str:
    r = rng if rng is not None else random
    template = r.choice(_WRAPPERS)
    return template.format(body=body, code=f"{r.randint(0, 9999):04d}")


async def _opted_in_chats(db: Database) -> list[int]:
    rows = await db.fetch(
        "SELECT chat_id FROM chat_config WHERE ether_enabled = TRUE",
    )
    return [r["chat_id"] for r in rows]


async def _pick_source_message(
    db: Database, source_chat_ids: list[int],
) -> tuple[int, str] | None:
    """Return (source_chat_id, message_text) or None if nothing eligible.

    Eligible = within lookback, role='user' (skip the bot's own + system),
    long enough to be interesting. Random pick across all eligible rows
    so a quiet chat with one good message has the same odds as a chatty
    one with twenty.
    """
    if not source_chat_ids:
        return None
    since = datetime.now(timezone.utc) - _SOURCE_LOOKBACK
    row = await db.fetchrow(
        "SELECT chat_id, content FROM messages "
        " WHERE chat_id = ANY($1::bigint[]) "
        "   AND role = 'user' "
        "   AND created_at >= $2 "
        "   AND char_length(content) >= $3 "
        " ORDER BY random() LIMIT 1",
        source_chat_ids, since, _MIN_MESSAGE_LEN,
    )
    if row is None:
        return None
    return int(row["chat_id"]), row["content"]


async def _pick_destination(
    db: Database, all_opted_in: list[int], exclude: int,
) -> int | None:
    """Pick a different opted-in chat that isn't on receiver cooldown."""
    candidates = [c for c in all_opted_in if c != exclude]
    if not candidates:
        return None
    cutoff = datetime.now(timezone.utc) - _RECEIVER_COOLDOWN
    rows = await db.fetch(
        "SELECT c.chat_id FROM chats c "
        "LEFT JOIN chat_state cs ON cs.chat_id = c.chat_id "
        " WHERE c.chat_id = ANY($1::bigint[]) "
        "   AND (cs.last_ether_at IS NULL OR cs.last_ether_at < $2)",
        candidates, cutoff,
    )
    eligible = [int(r["chat_id"]) for r in rows]
    if not eligible:
        return None
    return random.choice(eligible)


def _pick_destination_any(all_opted_in: list[int], exclude: int) -> int | None:
    """Pick any opted-in chat other than ``exclude``, ignoring cooldown.

    Used by the manual /ether command: a deliberate transmission isn't
    rate-limited by the receiver's auto-loop cooldown. We still only land
    in chats that have opted into the ether network.
    """
    candidates = [c for c in all_opted_in if c != exclude]
    return random.choice(candidates) if candidates else None


async def _stamp_receiver(db: Database, dest_id: int) -> None:
    """Record that ``dest_id`` just received a transmission so the auto
    loop respects the 4h cooldown afterwards."""
    await db.execute(
        "INSERT INTO chat_state (chat_id, last_ether_at) "
        "VALUES ($1, NOW()) "
        "ON CONFLICT (chat_id) DO UPDATE "
        "SET last_ether_at = EXCLUDED.last_ether_at",
        dest_id,
    )


@dataclass
class ManualEtherResult:
    """Outcome of a manual /ether transmission.

    ``mode`` is one of:
      * ``"voice"``   — radio-treated audio was sent to ``dest_id``
      * ``"text"``    — audio path unavailable; garbled text sent instead
      * ``"no_dest"`` — no other ether-enabled chat to send to
      * ``"no_audio"``— a voice note was given but ffmpeg couldn't process
                        it (and there's no text to fall back to)
    """
    mode: str
    dest_id: int | None = None


async def manual_broadcast(
    bot: Bot,
    db: Database,
    openai,
    source_chat_id: int,
    *,
    text: str | None = None,
    voice_bytes: bytes | None = None,
) -> ManualEtherResult:
    """Transmit a user-supplied message into the ether as a radio voice.

    Resolution order for the audio:
      1. ``voice_bytes`` (a real voice note) → radio FX.
      2. ``text`` → TTS → radio FX.
    If the audio path fails and we have ``text``, fall back to a garbled
    text broadcast. Destination is a random *other* ether-enabled chat.
    """
    opted_in = await _opted_in_chats(db)
    dest_id = _pick_destination_any(opted_in, exclude=source_chat_id)
    if dest_id is None:
        return ManualEtherResult(mode="no_dest")

    # Radio audio leans heavy; the (rare) text fallback stays readable.
    radio_intensity = _roll_radio_intensity()

    # 1) Obtain the source audio (real recording, or TTS of the text).
    src_audio = voice_bytes
    if src_audio is None and text:
        src_audio = await openai.text_to_speech(text, chat_id=source_chat_id)

    # 2) Apply the radio effect and send as a voice note.
    if src_audio:
        treated = await apply_radio_effect(src_audio, intensity=radio_intensity)
        if treated:
            caption = random.choice(_VOICE_CAPTIONS)
            try:
                sent = await bot.send_voice(
                    dest_id,
                    BufferedInputFile(treated, filename="ether.ogg"),
                    caption=caption,
                    disable_notification=is_silenced(dest_id),
                )
                track(dest_id, sent.message_id, caption)
                await _stamp_receiver(db, dest_id)
                log.info(
                    "Ether voice: %s → %s (intensity=%.2f).",
                    source_chat_id, dest_id, radio_intensity,
                )
                return ManualEtherResult(mode="voice", dest_id=dest_id)
            except Exception as exc:  # pragma: no cover - telegram hiccup
                log.warning("Ether voice send failed → %s: %s", dest_id, exc)

    # 3) Fallbacks: text → garbled text broadcast; voice-only → give up.
    if text:
        body = garble_pager(text, intensity=_roll_intensity())
        msg_text = _wrap(body)
        try:
            sent = await bot.send_message(
                dest_id, msg_text,
                disable_notification=is_silenced(dest_id),
            )
            track(dest_id, sent.message_id, msg_text)
            await _stamp_receiver(db, dest_id)
            log.info("Ether text fallback: %s → %s.", source_chat_id, dest_id)
            return ManualEtherResult(mode="text", dest_id=dest_id)
        except Exception as exc:  # pragma: no cover
            log.warning("Ether text send failed → %s: %s", dest_id, exc)
            return ManualEtherResult(mode="no_audio", dest_id=dest_id)

    return ManualEtherResult(mode="no_audio", dest_id=dest_id)


async def broadcast_now(bot: Bot, db: Database) -> tuple[int, int] | None:
    """Force one ether broadcast, bypassing the per-tick dice roll.

    Returns ``(source_chat_id, dest_chat_id)`` on success or ``None`` if
    there's no eligible source message, no idle destination, or fewer
    than two opted-in chats. Used by ``_maybe_broadcast`` after rolling
    dice, and by ``/debug_ether`` to verify the path end-to-end.
    """
    opted_in = await _opted_in_chats(db)
    if len(opted_in) < 2:
        return None
    picked = await _pick_source_message(db, opted_in)
    if picked is None:
        return None
    source_id, raw = picked
    dest_id = await _pick_destination(db, opted_in, exclude=source_id)
    if dest_id is None:
        return None
    intensity = _roll_intensity()
    body = garble_pager(raw, intensity=intensity)
    text = _wrap(body)
    try:
        sent = await bot.send_message(
            dest_id, text,
            disable_notification=is_silenced(dest_id),
        )
        track(dest_id, sent.message_id, text)
        await db.execute(
            "INSERT INTO chat_state (chat_id, last_ether_at) "
            "VALUES ($1, NOW()) "
            "ON CONFLICT (chat_id) DO UPDATE "
            "SET last_ether_at = EXCLUDED.last_ether_at",
            dest_id,
        )
        log.info(
            "Ether broadcast: %s → %s (intensity=%.2f).",
            source_id, dest_id, intensity,
        )
        return source_id, dest_id
    except Exception as exc:  # pragma: no cover
        log.warning("Ether broadcast failed (%s → %s): %s", source_id, dest_id, exc)
        return None


async def _maybe_broadcast(bot: Bot, db: Database) -> None:
    if random.random() >= _DROP_CHANCE:
        return
    await broadcast_now(bot, db)


async def run_ether_loop(bot: Bot, db: Database, stop: asyncio.Event) -> None:
    log.info("Ether loop running.")
    while not stop.is_set():
        try:
            await _maybe_broadcast(bot, db)
            wait = _TICK_SECONDS
        except Exception as exc:
            log.exception("Ether loop iteration failed: %s", exc)
            wait = _TICK_SECONDS
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    log.info("Ether loop stopped.")
