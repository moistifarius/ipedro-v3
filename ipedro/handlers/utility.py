"""General-purpose user commands.

/remind, /poll, /whatdid, /mood, /quote(s), /unquote, /tldr,
/birthday, /anniversary, /dates, /catchphrases, /lexicon, /heatmap.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

from ipedro.handlers.common import display_name, get_or_create_chat_config
from ipedro.prompts import (
    COMPLIMENT_PROMPT, ECHO_PROMPT, HAIKU_PROMPT, MISHEARD_LYRIC_PROMPT,
    ROAST_PROMPT, THIS_OR_THAT_PROMPT, TLDR_PROMPT,
)
from ipedro.reminders import add_reminder, parse_duration
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

_WHATDID_PROMPT = (
    "Generate a confident, slightly exaggerated 2-3 sentence summary of "
    "what {name} has been up to in this chat lately, based ONLY on the "
    "messages below. Be playful, slightly suspicious, and present small "
    "things as if they were big deals. Do NOT quote the messages directly; "
    "synthesize. If there is nothing in the messages, invent a single "
    "absurd theory about what they've been up to elsewhere.\n\n"
    "Messages from {name}:\n{messages}"
)

# English-ish stopwords for /lexicon and /catchphrases.
_STOPWORDS = frozenset((
    "the a an and or but if then so of to in on at for with by from as is are "
    "was were be been being have has had do does did will would could should "
    "i you he she it we they me him her them my your his its our their this "
    "that these those there here what when where why how who which not no yes "
    "just like get got go going make made one two too also very really only "
    "can may might must about into out up down over under more most some any "
    "im ive id youre dont didnt wont cant couldnt shouldnt thats whats hes "
    "shes were arent isnt wasnt werent havent hasnt hadnt ill youll well "
    "theyll theres heres lol lmao haha ok okay yeah yep nope nah"
).split())

_WORD_RE = re.compile(r"[A-Za-z']+")
_DATE_FORMATS = (
    "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%Y", "%m-%d", "%m/%d", "%d %b", "%b %d",
    "%B %d", "%d %B",
)


def _parse_user_date(raw: str) -> tuple[int, int, int | None] | None:
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            year = dt.year if "%Y" in fmt else None
            return dt.month, dt.day, year
        except ValueError:
            continue
    return None


async def _resolve_target_user(
    rt: Runtime, msg: Message,
) -> tuple[int | None, str]:
    """Reply-to wins; else @username; else None. Returns (user_id, display)."""
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return u.id, display_name(u)
    parts = (msg.text or "").split(None, 2)
    if len(parts) >= 2:
        arg = parts[1].strip().lstrip("@")
        row = await rt.db.fetchrow(
            "SELECT user_id, first_name, last_name, username "
            "  FROM users WHERE LOWER(username) = LOWER($1) LIMIT 1",
            arg,
        )
        if row:
            name = (
                f"{row['first_name'] or ''} {row['last_name'] or ''}"
            ).strip() or row["username"] or arg
            return row["user_id"], name
    return None, ""


def build_router(rt: Runtime) -> Router:
    r = Router(name="utility")

    @r.message(Command("remind"))
    async def remind(msg: Message) -> None:
        """/remind <duration> <text> — e.g. /remind 1h30m feed the cats."""
        raw = msg.text or ""
        parts = raw.split(None, 2)
        if len(parts) < 3:
            await msg.reply(
                "Usage: /remind <duration> <text>\n"
                "Duration: 30s, 5m, 1h, 2h30m, 1d, 1w (any combo).",
                disable_notification=True,
            )
            return
        seconds = parse_duration(parts[1])
        if seconds is None:
            await msg.reply(
                "Couldn't parse the duration. Try things like '5m', '1h30m', '2d'.",
                disable_notification=True,
            )
            return
        body = parts[2].strip()
        if not body:
            await msg.reply("Empty reminder text.", disable_notification=True)
            return
        rid = await add_reminder(
            rt.db, msg.chat.id,
            msg.from_user.id if msg.from_user else None,
            body, seconds,
        )
        await msg.reply(
            f"⏰ Set. I'll remind you in {parts[1]} (#{rid}).",
            disable_notification=True,
        )

    @r.message(Command("whatdid"))
    async def whatdid(msg: Message) -> None:
        """Confidently summarize what a user has been up to."""
        await get_or_create_chat_config(rt, msg)
        target_user_id: int | None = None
        target_name = "they"
        # Reply-to wins; else parse @username from the arg.
        if msg.reply_to_message and msg.reply_to_message.from_user:
            u = msg.reply_to_message.from_user
            target_user_id = u.id
            target_name = display_name(u)
        else:
            parts = (msg.text or "").split(None, 1)
            if len(parts) >= 2:
                arg = parts[1].strip().lstrip("@")
                row = await rt.db.fetchrow(
                    "SELECT user_id, first_name, last_name, username "
                    "  FROM users WHERE LOWER(username) = LOWER($1) LIMIT 1",
                    arg,
                )
                if row:
                    target_user_id = row["user_id"]
                    target_name = (
                        f"{row['first_name'] or ''} {row['last_name'] or ''}"
                    ).strip() or row["username"] or arg
        if target_user_id is None:
            await msg.reply(
                "Usage: /whatdid @username  (or reply to someone with /whatdid).",
                disable_notification=True,
            )
            return

        rows = await rt.db.fetch(
            "SELECT content FROM messages "
            " WHERE chat_id = $1 AND user_id = $2 "
            " ORDER BY id DESC LIMIT 30",
            msg.chat.id, target_user_id,
        )
        joined = "\n".join(f"- {r['content']}" for r in reversed(rows)) or "(none)"

        await rt.bot.send_chat_action(msg.chat.id, "typing")
        out = await rt.openai.short_completion(
            _WHATDID_PROMPT.format(name=target_name, messages=joined),
            max_tokens=200,
        )
        await msg.reply(
            out or f"No idea what {target_name} has been doing.",
            disable_notification=True,
        )

    @r.message(Command("mood"))
    async def mood(msg: Message) -> None:
        """Show this chat's current persona state (mood, word-of-the-day, stuck word)."""
        await get_or_create_chat_config(rt, msg)
        state = await rt.persona_state.current(msg.chat.id)
        lines = [f"Mood: {state.mood or 'unset'}"]
        if state.word_of_day:
            lines.append(f"Word of the day: {state.word_of_day}")
        if state.stuck_word:
            lines.append(f"Currently stuck on: {state.stuck_word}")
        await msg.reply("\n".join(lines), disable_notification=True)

    @r.message(Command("poll"))
    async def poll(msg: Message) -> None:
        """/poll Question | Option A | Option B | ... (2-10 options)."""
        raw = (msg.text or "").split(None, 1)
        if len(raw) < 2:
            await msg.reply(
                "Usage: /poll Question | Option A | Option B | ...",
                disable_notification=True,
            )
            return
        parts = [p.strip() for p in raw[1].split("|") if p.strip()]
        if len(parts) < 3:
            await msg.reply(
                "Need a question and at least two options, separated by |.",
                disable_notification=True,
            )
            return
        question, options = parts[0], parts[1:11]  # Telegram caps at 10
        try:
            await rt.bot.send_poll(
                chat_id=msg.chat.id,
                question=question[:300],
                options=[o[:100] for o in options],
                is_anonymous=True,
            )
        except Exception as exc:
            await msg.reply(f"Poll failed: {exc}", disable_notification=True)

    @r.message(Command("quote"))
    async def quote(msg: Message) -> None:
        """/quote (reply to a message) saves it; /quote on its own = random quote."""
        await get_or_create_chat_config(rt, msg)
        if msg.reply_to_message and (
            msg.reply_to_message.text or msg.reply_to_message.caption
        ):
            target = msg.reply_to_message
            body = (target.text or target.caption or "").strip()
            qname = display_name(target.from_user) if target.from_user else "anonymous"
            qid = await rt.db.fetchval(
                "INSERT INTO quotes (chat_id, quoted_user_id, quoted_name, "
                "                    text, saved_by, source_message_id) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
                msg.chat.id,
                target.from_user.id if target.from_user else None,
                qname, body[:2000],
                msg.from_user.id if msg.from_user else None,
                target.message_id,
            )
            await msg.reply(
                f"📜 Saved as quote #{qid}.", disable_notification=True,
            )
            return
        # No reply → random quote
        row = await rt.db.fetchrow(
            "SELECT id, quoted_name, text FROM quotes "
            " WHERE chat_id = $1 ORDER BY random() LIMIT 1",
            msg.chat.id,
        )
        if not row:
            await msg.reply(
                "No quotes saved yet. Reply to a message with /quote to save one.",
                disable_notification=True,
            )
            return
        await msg.reply(
            f"📜 #{row['id']} — {row['quoted_name']}:\n\"{row['text']}\"",
            disable_notification=True,
        )

    @r.message(Command("quotes"))
    async def quotes_list(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        rows = await rt.db.fetch(
            "SELECT id, quoted_name, text FROM quotes "
            " WHERE chat_id = $1 ORDER BY id DESC LIMIT 20",
            msg.chat.id,
        )
        if not rows:
            await msg.reply("No quotes yet.", disable_notification=True)
            return
        lines = [f"📜 #{r['id']} {r['quoted_name']}: {r['text'][:120]}" for r in rows]
        await msg.reply("\n".join(lines)[:4000], disable_notification=True)

    @r.message(Command("unquote"))
    async def unquote(msg: Message) -> None:
        parts = (msg.text or "").split()
        if len(parts) < 2:
            await msg.reply("Usage: /unquote <id>", disable_notification=True)
            return
        try:
            qid = int(parts[1])
        except ValueError:
            await msg.reply("Bad id.", disable_notification=True)
            return
        res = await rt.db.execute(
            "DELETE FROM quotes WHERE id = $1 AND chat_id = $2",
            qid, msg.chat.id,
        )
        deleted = int(res.split()[-1]) if res else 0
        await msg.reply(
            "Deleted." if deleted else "Not found in this chat.",
            disable_notification=True,
        )

    @r.message(Command("tldr"))
    async def tldr(msg: Message) -> None:
        """/tldr [duration] — summarize the last window (default 24h)."""
        await get_or_create_chat_config(rt, msg)
        parts = (msg.text or "").split()
        window_seconds = 86400
        if len(parts) >= 2:
            parsed = parse_duration(parts[1])
            if parsed is None:
                await msg.reply(
                    "Bad duration. Try /tldr 6h or /tldr 2d.",
                    disable_notification=True,
                )
                return
            window_seconds = parsed
        since = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        rows = await rt.db.fetch(
            "SELECT role, content FROM messages "
            " WHERE chat_id = $1 AND created_at >= $2 "
            " ORDER BY id ASC LIMIT 300",
            msg.chat.id, since,
        )
        if not rows:
            await msg.reply(
                "Nothing to summarize in that window.",
                disable_notification=True,
            )
            return
        joined = "\n".join(f"{r['role']}: {r['content']}" for r in rows)
        await rt.bot.send_chat_action(msg.chat.id, "typing")
        out = await rt.openai.short_completion(
            TLDR_PROMPT.format(messages=joined[:12000]), max_tokens=400,
        )
        await msg.reply(out or "(empty)", disable_notification=True)

    @r.message(Command("birthday"))
    async def birthday(msg: Message) -> None:
        await _set_date(rt, msg, label="birthday")

    @r.message(Command("anniversary"))
    async def anniversary(msg: Message) -> None:
        await _set_date(rt, msg, label="anniversary")

    @r.message(Command("dates"))
    async def dates(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        rows = await rt.db.fetch(
            "SELECT user_id, label, month, day, year, note "
            "  FROM chat_dates WHERE chat_id = $1 "
            " ORDER BY month, day",
            msg.chat.id,
        )
        if not rows:
            await msg.reply(
                "No dates tracked here yet. Use /birthday MM-DD or "
                "/anniversary <name> MM-DD-YYYY.",
                disable_notification=True,
            )
            return
        lines = []
        for r in rows:
            user_row = await rt.db.fetchrow(
                "SELECT first_name, username FROM users WHERE user_id = $1",
                r["user_id"],
            ) if r["user_id"] else None
            who = (
                (user_row and (user_row["first_name"] or user_row["username"]))
                or "—"
            )
            year_part = f"/{r['year']}" if r["year"] else ""
            note_part = f" ({r['note']})" if r["note"] else ""
            lines.append(
                f"{r['month']:02d}-{r['day']:02d}{year_part}  "
                f"{r['label']:<12} {who}{note_part}"
            )
        await msg.reply("\n".join(lines)[:4000], disable_notification=True)

    @r.message(Command("catchphrases"))
    async def catchphrases(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        user_id, name = await _resolve_target_user(rt, msg)
        if user_id is None:
            await msg.reply(
                "Usage: /catchphrases @username (or reply to someone).",
                disable_notification=True,
            )
            return
        rows = await rt.db.fetch(
            "SELECT content FROM messages "
            " WHERE chat_id = $1 AND user_id = $2 "
            " ORDER BY id DESC LIMIT 1000",
            msg.chat.id, user_id,
        )
        if not rows:
            await msg.reply(
                f"No messages from {name} here.", disable_notification=True,
            )
            return
        phrases: Counter[str] = Counter()
        for r in rows:
            tokens = [t.lower() for t in _WORD_RE.findall(r["content"])]
            tokens = [t for t in tokens if t not in _STOPWORDS and len(t) > 2]
            for n in (3, 4):
                for i in range(len(tokens) - n + 1):
                    phrases[" ".join(tokens[i:i + n])] += 1
        top = [(p, c) for p, c in phrases.most_common(50) if c >= 3][:10]
        if not top:
            await msg.reply(
                f"{name} doesn't repeat any phrases noticeably.",
                disable_notification=True,
            )
            return
        body = "\n".join(f"  ×{c:<3} {p}" for p, c in top)
        await msg.reply(
            f"{name}'s catchphrases:\n{body}", disable_notification=True,
        )

    @r.message(Command("lexicon"))
    async def lexicon(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        user_id, name = await _resolve_target_user(rt, msg)
        if user_id is None:
            await msg.reply(
                "Usage: /lexicon @username (or reply to someone).",
                disable_notification=True,
            )
            return
        rows = await rt.db.fetch(
            "SELECT content FROM messages "
            " WHERE chat_id = $1 AND user_id = $2 "
            " ORDER BY id DESC LIMIT 2000",
            msg.chat.id, user_id,
        )
        if not rows:
            await msg.reply(
                f"No messages from {name} here.", disable_notification=True,
            )
            return
        counts: Counter[str] = Counter()
        for r in rows:
            for t in _WORD_RE.findall(r["content"]):
                t = t.lower()
                if len(t) <= 2 or t in _STOPWORDS:
                    continue
                counts[t] += 1
        top = counts.most_common(20)
        if not top:
            await msg.reply(
                f"{name} hasn't said much.", disable_notification=True,
            )
            return
        body = "\n".join(f"  ×{c:<4} {w}" for w, c in top)
        await msg.reply(
            f"{name}'s top words:\n{body}", disable_notification=True,
        )

    @r.message(Command("heatmap"))
    async def heatmap(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        rows = await rt.db.fetch(
            "SELECT EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC')::int AS h, "
            "       COUNT(*) AS c "
            "  FROM messages WHERE chat_id = $1 "
            " GROUP BY h ORDER BY h",
            msg.chat.id,
        )
        if not rows:
            await msg.reply(
                "Not enough chat history.", disable_notification=True,
            )
            return
        by_hour = {int(r["h"]): int(r["c"]) for r in rows}
        peak = max(by_hour.values())
        lines = ["Hour-of-day activity (UTC):"]
        for h in range(24):
            c = by_hour.get(h, 0)
            bar_len = int((c / peak) * 24) if peak else 0
            lines.append(f"  {h:02d}  {'█' * bar_len}{'·' * (24 - bar_len)}  {c}")
        await msg.reply("\n".join(lines), disable_notification=True)

    @r.message(Command("haiku"))
    async def haiku(msg: Message) -> None:
        """/haiku — compose a haiku about the recent chat."""
        await get_or_create_chat_config(rt, msg)
        rows = await rt.db.fetch(
            "SELECT role, content FROM messages "
            " WHERE chat_id = $1 ORDER BY id DESC LIMIT 20",
            msg.chat.id,
        )
        snippet = "\n".join(
            f"{r['role']}: {r['content']}" for r in reversed(rows)
        ) or "(silence)"
        await rt.bot.send_chat_action(msg.chat.id, "typing")
        out = await rt.openai.short_completion(
            HAIKU_PROMPT.format(messages=snippet[:4000]),
            max_tokens=120, chat_id=msg.chat.id,
        )
        await msg.reply(out or "🪷", disable_notification=True)

    @r.message(Command("this_or_that"))
    async def this_or_that(msg: Message) -> None:
        """/this_or_that A | B — the Dude decides dramatically."""
        await get_or_create_chat_config(rt, msg)
        raw = (msg.text or "").split(None, 1)
        if len(raw) < 2 or "|" not in raw[1]:
            await msg.reply(
                "Usage: /this_or_that option A | option B",
                disable_notification=True,
            )
            return
        a, b = (s.strip() for s in raw[1].split("|", 1))
        if not a or not b:
            await msg.reply(
                "Both options need to be non-empty.",
                disable_notification=True,
            )
            return
        await rt.bot.send_chat_action(msg.chat.id, "typing")
        out = await rt.openai.cheap_completion(
            THIS_OR_THAT_PROMPT.format(a=a, b=b),
            max_tokens=120, chat_id=msg.chat.id,
        )
        await msg.reply(out or "Both. No, neither.", disable_notification=True)

    @r.message(Command("echo"))
    async def echo(msg: Message) -> None:
        """/echo @user [topic] — the Dude mimics that user's style."""
        await get_or_create_chat_config(rt, msg)
        user_id, name = await _resolve_target_user(rt, msg)
        if user_id is None:
            await msg.reply(
                "Usage: /echo @username [topic]  (or reply to someone).",
                disable_notification=True,
            )
            return
        # Topic = everything after the @user token (or "anything" if reply-to).
        parts = (msg.text or "").split(None, 2)
        topic = parts[2] if len(parts) >= 3 else "literally anything they'd say"
        rows = await rt.db.fetch(
            "SELECT content FROM messages "
            " WHERE chat_id = $1 AND user_id = $2 "
            " ORDER BY id DESC LIMIT 40",
            msg.chat.id, user_id,
        )
        if not rows:
            await msg.reply(
                f"No messages from {name} to mimic.",
                disable_notification=True,
            )
            return
        examples = "\n".join(f"- {r['content']}" for r in rows)
        await rt.bot.send_chat_action(msg.chat.id, "typing")
        out = await rt.openai.cheap_completion(
            ECHO_PROMPT.format(name=name, topic=topic, messages=examples[:4000]),
            max_tokens=200, chat_id=msg.chat.id,
        )
        if not out:
            await msg.reply("(couldn't echo)", disable_notification=True)
            return
        await msg.reply(f"[{name} voice] {out}", disable_notification=True)

    @r.message(Command("roast"))
    async def roast(msg: Message) -> None:
        await _do_burn(rt, msg, prompt=ROAST_PROMPT, fallback="(couldn't roast)")

    @r.message(Command("compliment"))
    async def compliment(msg: Message) -> None:
        await _do_burn(rt, msg, prompt=COMPLIMENT_PROMPT, fallback="(couldn't compliment)")

    @r.message(Command("confess"))
    async def confess(msg: Message) -> None:
        """Submit an anonymous confession (DM only)."""
        if msg.chat and msg.chat.type != "private":
            try:
                await msg.delete()
            except Exception:
                pass
            try:
                if msg.from_user:
                    await rt.bot.send_message(
                        msg.from_user.id,
                        "Send /confess in this DM — never in a group, "
                        "or you defeat the anonymous part.",
                    )
            except Exception:
                pass
            return
        parts = (msg.text or "").split(None, 1)
        if len(parts) < 2:
            await msg.reply(
                "Usage: /confess <your anonymous text>",
                disable_notification=True,
            )
            return
        body = parts[1].strip()[:1000]
        if not body:
            return
        cid = await rt.db.fetchval(
            "INSERT INTO confessions (submitted_by, text) "
            "VALUES ($1, $2) RETURNING id",
            msg.from_user.id if msg.from_user else None, body,
        )
        await msg.reply(
            f"📩 Stored anonymously (#{cid}). I might surface it. Or might not.",
            disable_notification=True,
        )

    @r.message(Command("lyric"))
    async def lyric(msg: Message) -> None:
        """/lyric <line> — the Dude confidently mishears the lyric."""
        await get_or_create_chat_config(rt, msg)
        parts = (msg.text or "").split(None, 1)
        if len(parts) < 2:
            await msg.reply(
                "Usage: /lyric <a line of lyrics>",
                disable_notification=True,
            )
            return
        line = parts[1].strip()[:300]
        await rt.bot.send_chat_action(msg.chat.id, "typing")
        out = await rt.openai.cheap_completion(
            MISHEARD_LYRIC_PROMPT.format(line=line),
            max_tokens=120, chat_id=msg.chat.id,
        )
        if not out:
            await msg.reply("(my ears are clean today)", disable_notification=True)
            return
        await msg.reply(
            f"I'm pretty sure they actually say:\n\n\"{out}\"",
            disable_notification=True,
        )

    @r.message(Command("whoslurking"))
    async def whoslurking(msg: Message) -> None:
        """/whoslurking — users who've spoken here but not in the last 7 days."""
        await get_or_create_chat_config(rt, msg)
        rows = await rt.db.fetch(
            """
            SELECT m.user_id,
                   MAX(m.created_at) AS last_seen,
                   COALESCE(MAX(u.first_name), MAX(u.username)) AS name
              FROM messages m
              LEFT JOIN users u ON u.user_id = m.user_id
             WHERE m.chat_id = $1 AND m.user_id IS NOT NULL
             GROUP BY m.user_id
            HAVING MAX(m.created_at) < NOW() - INTERVAL '7 days'
             ORDER BY last_seen DESC
             LIMIT 20
            """,
            msg.chat.id,
        )
        if not rows:
            await msg.reply(
                "No lurkers. Everyone's recently said something.",
                disable_notification=True,
            )
            return
        lines = ["👻 Lurkers (no messages in 7+ days):"]
        for r_ in rows:
            since = r_["last_seen"].strftime("%Y-%m-%d")
            lines.append(f"  {r_['name'] or r_['user_id']} — last seen {since}")
        await msg.reply("\n".join(lines), disable_notification=True)

    @r.message(Command("meme"))
    async def meme(msg: Message) -> None:
        """/meme top text | bottom text (or just a single line)."""
        await get_or_create_chat_config(rt, msg)
        raw = (msg.text or "").split(None, 1)
        if len(raw) < 2:
            await msg.reply(
                "Usage: /meme top text | bottom text",
                disable_notification=True,
            )
            return
        body = raw[1].strip()
        parts = [p.strip() for p in body.split("|", 1)]
        top = parts[0]
        bottom = parts[1] if len(parts) > 1 else ""
        prompt = (
            f"A classic impact-font meme image. Bold white text with black "
            f"outline. TOP TEXT: \"{top}\". BOTTOM TEXT: \"{bottom}\". "
            f"Choose a fitting absurd or iconic background image; keep the "
            f"text large, centered, all caps, legible."
            if bottom else
            f"A classic impact-font meme image with bold white-on-black "
            f"caption: \"{top}\". Bold absurd background that matches. "
            f"All caps, centered, legible text."
        )
        await rt.bot.send_chat_action(msg.chat.id, "upload_photo")
        data = await rt.openai.generate_image(prompt, chat_id=msg.chat.id)
        if not data:
            await msg.reply("Meme failed.", disable_notification=True)
            return
        await msg.reply_photo(
            BufferedInputFile(data, filename="meme.png"),
            caption=body[:200],
            disable_notification=True,
        )

    @r.message(Command("config"))
    async def config_wizard(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        cfg = await rt.chats.get_config(msg.chat.id)
        await msg.reply(
            _config_wizard_header(cfg, msg.chat.id, is_dm_scoped=False),
            reply_markup=_config_keyboard(cfg, target_chat_id=msg.chat.id),
            disable_notification=True,
        )

    @r.callback_query(F.data.startswith("cfg:"))
    async def on_cfg(cb: CallbackQuery) -> None:
        if not cb.data or not cb.message:
            return
        # New callback shape: ``cfg:<target_chat_id>:<rest>``. The target
        # chat id encoded in the callback drives the edits, NOT the chat
        # that hosts the wizard message — that lets the admin run the
        # wizard from their DM scoped to any chat (see /config_for).
        parts = cb.data.split(":", 2)
        if len(parts) < 3:
            await cb.answer("Stale wizard — re-open /config.", show_alert=True)
            return
        try:
            target_chat_id = int(parts[1])
        except ValueError:
            await cb.answer("Stale wizard — re-open /config.", show_alert=True)
            return
        field = parts[2]
        if field == "noop":
            await cb.answer()
            return
        cfg = await rt.chats.get_config(target_chat_id)
        if not cfg:
            await cb.answer("Run /config first.", show_alert=True)
            return
        toggles = {
            "duckhunt": ("duckhunt_enabled", not cfg.duckhunt_enabled),
            "sharephoto": ("share_photo_enabled", not cfg.share_photo_enabled),
            "comic": ("comic_enabled", not cfg.comic_enabled),
            "fortune": ("fortune_enabled", not cfg.fortune_enabled),
            "voice": ("voice_transcribe", not cfg.voice_transcribe),
            "memory": ("memory_enabled", not cfg.memory_enabled),
            "ether": ("ether_enabled", not cfg.ether_enabled),
        }
        if field in toggles:
            col, new_val = toggles[field]
            await rt.chats.update_config(target_chat_id, **{col: new_val})
        elif field.startswith("policy:"):
            new_policy = field.split(":", 1)[1]
            if new_policy in ("commands", "mention", "reply", "ambient", "always"):
                await rt.chats.update_config(
                    target_chat_id, response_policy=new_policy,
                )
        elif field.startswith("persona:"):
            new_persona = field.split(":", 1)[1]
            if new_persona in ("dude", "pedro", "neutral"):
                await rt.chats.update_config(
                    target_chat_id, persona=new_persona, persona_custom=None,
                )
        new_cfg = await rt.chats.get_config(target_chat_id)
        # DM-scoped means the cb.message.chat.id (where the wizard is shown)
        # differs from the target_chat_id (whose config we're editing).
        is_dm_scoped = cb.message.chat.id != target_chat_id
        try:
            await cb.message.edit_text(
                _config_wizard_header(new_cfg, target_chat_id, is_dm_scoped=is_dm_scoped),
                reply_markup=_config_keyboard(new_cfg, target_chat_id=target_chat_id),
            )
        except Exception:
            pass
        await cb.answer()

    return r


def _config_wizard_header(cfg, target_chat_id: int, *, is_dm_scoped: bool) -> str:
    """Build the header line shown above the wizard keyboard.

    DM-scoped renders surface the target chat id so the admin can see at a
    glance which chat they're editing. In-group renders keep the original
    short label.
    """
    if is_dm_scoped:
        return f"⚙️ Settings for chat {target_chat_id}:"
    return "⚙️ Chat settings:"


def _config_keyboard(cfg, *, target_chat_id: int) -> InlineKeyboardMarkup:
    """Build the /config wizard keyboard. Mirrors fields editable via /chat_config.

    target_chat_id is encoded into every callback so the handler can edit the
    config row of an arbitrary chat (used by the DM-scoped /config_for flow).
    """

    def b(label: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=label, callback_data=f"cfg:{target_chat_id}:{data}",
        )

    def noop_btn(label: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=label, callback_data=f"cfg:{target_chat_id}:noop",
        )

    on = "ON"
    off = "off"

    rows = [
        [
            b(f"Duckhunt: {on if cfg.duckhunt_enabled else off}", "duckhunt"),
            b(f"Sharephoto: {on if cfg.share_photo_enabled else off}", "sharephoto"),
        ],
        [
            b(f"Comic: {on if cfg.comic_enabled else off}", "comic"),
            b(f"Fortune: {on if cfg.fortune_enabled else off}", "fortune"),
        ],
        [
            b(f"Voice: {on if cfg.voice_transcribe else off}", "voice"),
            b(f"Memory: {on if cfg.memory_enabled else off}", "memory"),
        ],
        [
            b(f"📟 Ether: {on if cfg.ether_enabled else off}", "ether"),
        ],
        [
            noop_btn(f"Policy: {cfg.response_policy}"),
        ],
        [
            b("commands", "policy:commands"),
            b("mention", "policy:mention"),
            b("reply", "policy:reply"),
        ],
        [
            b("ambient", "policy:ambient"),
            b("always", "policy:always"),
        ],
        [
            noop_btn(f"Persona: {cfg.persona}"),
        ],
        [
            b("dude", "persona:dude"),
            b("neutral", "persona:neutral"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _do_burn(
    rt: Runtime, msg: Message, *, prompt: str, fallback: str,
) -> None:
    """Shared body for /roast and /compliment."""
    await get_or_create_chat_config(rt, msg)
    user_id, name = await _resolve_target_user(rt, msg)
    if user_id is None:
        await msg.reply(
            "Usage: that command needs an @user (or reply to them).",
            disable_notification=True,
        )
        return
    rows = await rt.db.fetch(
        "SELECT content FROM messages "
        " WHERE chat_id = $1 AND user_id = $2 "
        " ORDER BY id DESC LIMIT 30",
        msg.chat.id, user_id,
    )
    examples = "\n".join(f"- {r['content']}" for r in rows) or "(no history)"
    await rt.bot.send_chat_action(msg.chat.id, "typing")
    out = await rt.openai.cheap_completion(
        prompt.format(name=name, messages=examples[:4000]),
        max_tokens=200, chat_id=msg.chat.id,
    )
    await msg.reply(out or fallback, disable_notification=True)


async def _set_date(rt: Runtime, msg: Message, *, label: str) -> None:
    """Shared body for /birthday and /anniversary.

    Forms:
      /birthday MM-DD                       — set caller's birthday
      /birthday @user MM-DD-YYYY            — set someone else's
      /anniversary "wedding" MM-DD-YYYY     — set chat-level anniversary
    """
    await get_or_create_chat_config(rt, msg)
    parts = (msg.text or "").split(None, 3)
    if len(parts) < 2:
        await msg.reply(
            f"Usage: /{label} MM-DD  (or /{label} @user MM-DD-YYYY)",
            disable_notification=True,
        )
        return
    target_user_id: int | None = msg.from_user.id if msg.from_user else None
    note: str | None = None
    arg_tokens: list[str] = []
    for tok in parts[1:]:
        if tok.startswith("@"):
            row = await rt.db.fetchrow(
                "SELECT user_id FROM users WHERE LOWER(username) = LOWER($1)",
                tok[1:],
            )
            target_user_id = row["user_id"] if row else None
        else:
            arg_tokens.append(tok)
    if not arg_tokens:
        await msg.reply("Need a date.", disable_notification=True)
        return
    date_tok = arg_tokens[-1]
    if len(arg_tokens) > 1:
        note = " ".join(arg_tokens[:-1])[:60]
    parsed = _parse_user_date(date_tok)
    if not parsed:
        await msg.reply(
            "Couldn't parse the date. Try MM-DD or MM-DD-YYYY.",
            disable_notification=True,
        )
        return
    month, day, year = parsed
    await rt.db.execute(
        "INSERT INTO chat_dates (chat_id, user_id, label, month, day, year, note) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) "
        "ON CONFLICT (chat_id, user_id, label) DO UPDATE "
        "SET month = EXCLUDED.month, day = EXCLUDED.day, year = EXCLUDED.year, "
        "    note = EXCLUDED.note",
        msg.chat.id, target_user_id, label, month, day, year, note,
    )
    await msg.reply(
        f"Got it: {label} on {month:02d}-{day:02d}"
        f"{f'/{year}' if year else ''}.",
        disable_notification=True,
    )
