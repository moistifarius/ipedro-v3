"""Admin-gated /debug_* commands to force-trigger the new behaviors.

These exist purely so the admin can verify each path end-to-end without
waiting on Poisson clocks or random outcomes. They are NOT registered if
the caller isn't an admin (require_admin handles that silently in groups).
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ipedro import radio_fx
from ipedro.ether import broadcast_now as ether_broadcast_now
from ipedro.handlers.common import display_name, require_admin
from ipedro.handlers.duckhunt import _issue_bef_challenge
from ipedro.runtime import Runtime
from ipedro.sharephoto import _take_and_post_photo

log = logging.getLogger(__name__)

_HELP = (
    "Debug commands (admin only):\n"
    "  /debug_help — this list\n"
    "  /debug_sharephoto — force Dale to generate + post a photo now\n"
    "  /debug_redditmeme — diagnose why /redditmeme is or isn't working "
    "(oauth mode, token, HTTP status)\n"
    "  /debug_ether — force one ether broadcast right now (needs ≥ 2 ether-enabled chats)\n"
    "  /ether_status — show which interference source (live WebSDR / bundled / synthetic) the last ether used, and cache state\n"
    "  /ether_refresh — drop the live shortwave cache; next /ether will refetch from the WebSDR list\n"
    "  /debug_challenge — issue a random bef challenge (captcha|trivia|recipe)\n"
    "  /debug_captcha — issue a captcha challenge\n"
    "  /debug_trivia — issue a trivia challenge\n"
    "  /debug_recipe — issue a recipe challenge\n"
    "  /debug_clear_challenge [chat_id] — clear stuck bef challenge(s) in a "
    "chat (default: this chat). Unsticks a chat where every message is "
    "judged 'Not quite. Try again.'\n"
    "  /debug_persona — show the exact system prompt this chat would use, "
    "and which layer (master_prompt / persona_custom / NEUTRAL) won the "
    "resolution. Use when '/master_prompt reset' seems to do nothing.\n"
    "  /debug_duck — alias for /duckhunt (force-spawn in this chat)\n"
    "\nFor the others, just type the trigger:\n"
    "  say 'dale' / 'dale gribble' / 'rusty shackleford' / 'idale' — Dale should reply\n"
    "  say 'cat' / 'kitty' / 🐈 — Dale drops a dubious cat fact\n"
    "  type 'bang' twice within 15s — second one trips the cooldown challenge"
)


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    s = int(seconds)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s ago"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m ago"


def build_router(rt: Runtime) -> Router:
    r = Router(name="debug")

    @r.message(Command("debug_help"))
    async def debug_help(msg: Message) -> None:
        if not await require_admin(msg, rt.settings.admin_ids):
            return
        await msg.reply(_HELP, disable_notification=True)

    @r.message(Command("debug_sharephoto"))
    async def debug_sharephoto(msg: Message) -> None:
        if not await require_admin(msg, rt.settings.admin_ids):
            return
        await msg.reply("Generating photo…", disable_notification=True)
        await _take_and_post_photo(msg.chat.id, rt.bot, rt.openai)

    @r.message(Command("debug_redditmeme"))
    async def debug_redditmeme(msg: Message) -> None:
        """Diagnose why /redditmeme is (or isn't) working."""
        if not await require_admin(msg, rt.settings.admin_ids):
            return
        from ipedro.reddit import diagnose
        rep = await diagnose(
            user_agent=rt.settings.reddit_user_agent,
            client_id=rt.settings.reddit_client_id,
            client_secret=rt.settings.reddit_client_secret,
        )
        lines = [
            "🔎 /redditmeme diagnosis",
            f"  credentials set: {rep['credentials_set']}",
            f"  mode: {rep['mode']}",
            f"  user-agent: {rep['user_agent']}",
        ]
        if rep["credentials_set"]:
            lines.append(f"  oauth token: {'ok' if rep['token_ok'] else 'FAILED'}")
        lines.append(f"  listing HTTP status: {rep['listing_status']}")
        if rep["listing_children"] is not None:
            lines.append(f"  posts returned: {rep['listing_children']}")
        if rep["error"]:
            lines.append(f"  error: {rep['error']}")
        # Actionable hint.
        if not rep["credentials_set"]:
            lines.append(
                "\nNo Reddit app credentials. Reddit blocks anonymous access "
                "from servers (403). Create a 'script' app at "
                "https://www.reddit.com/prefs/apps and set REDDIT_CLIENT_ID + "
                "REDDIT_CLIENT_SECRET in .env."
            )
        elif rep["credentials_set"] and not rep["token_ok"]:
            lines.append(
                "\nToken request failed — check REDDIT_CLIENT_ID / "
                "REDDIT_CLIENT_SECRET are correct and the app is a 'script' "
                "type (which has a secret)."
            )
        elif rep["listing_status"] and rep["listing_status"] != 200:
            lines.append(
                f"\nReddit returned {rep['listing_status']} on a data call. "
                "429 = rate-limited (wait); 401 = token/scope issue; 403 = "
                "still blocked."
            )
        await msg.reply("\n".join(lines), disable_notification=True)

    @r.message(Command("debug_ether"))
    async def debug_ether(msg: Message) -> None:
        if not await require_admin(msg, rt.settings.admin_ids):
            return
        result = await ether_broadcast_now(rt.bot, rt.db)
        if result is None:
            await msg.reply(
                "Ether broadcast couldn't fire — need ≥ 2 ether-enabled "
                "chats with a recent eligible message in one and an "
                "idle (no 4h cooldown) destination in another.",
                disable_notification=True,
            )
            return
        source_id, dest_id = result
        # Reports which interference source actually fed the bed, so the
        # admin can confirm the WebSDR fetch is hot vs falling back.
        bed = radio_fx.last_bed_source() or "(unknown)"
        await msg.reply(
            f"📟 Ether: {source_id} → {dest_id}. Interference: {bed}.",
            disable_notification=True,
        )

    @r.message(Command("ether_status"))
    async def ether_status(msg: Message) -> None:
        """Show whether the live WebSDR fetch is actually feeding /ether."""
        if not await require_admin(msg, rt.settings.admin_ids):
            return
        s = radio_fx.live_cache_status()
        urls: tuple[str, ...] = s["urls"]   # type: ignore[assignment]
        if not urls:
            url_block = (
                "  (live fetch is OFF — no RADIO_FX_LIVE_URLS configured.\n"
                "  Supported URL schemes:\n"
                "    kiwi://host:port?freq=14040&mode=lsb  — KiwiSDR client\n"
                "    http(s)://...                          — direct audio\n"
                "  Set RADIO_FX_LIVE_URLS=<url[,url2,...]> in .env to enable.)"
            )
            cache_state = ""
        else:
            lines = []
            for i, u in enumerate(urls, 1):
                marker = "  ★" if u == s.get("cached_source") else "   "
                lines.append(f"{marker} {i}. {u}")
            url_block = "\n".join(lines)
            cache_state = (
                f"\n  cached: {s['cached_seconds']:.1f}s "
                f"from {s['cached_source']} "
                f"({_format_age(s['cached_age_seconds'])}, "
                f"refresh after {int(s['ttl_seconds']/3600)}h)"
                if s["cached"] else
                "\n  cached: (empty — next /ether will try to fetch)"
            )
        bundled = int(s["bundled_count"])
        bundled_state = (
            f"  bundled assets: {bundled} shortwave_*.ogg in ipedro/assets/"
            if bundled else
            "  bundled assets: (none — drop shortwave_*.ogg into\n"
            "  ipedro/assets/ to use a real recording as the bed)"
        )
        last = s.get("last_bed_source") or "(no ether broadcast yet)"
        live_header = (
            "Live audio sources (priority 1):"
            if urls else
            "Live audio fetch:"
        )
        body = (
            "📡 Ether interference status\n\n"
            f"{live_header}\n"
            f"{url_block}{cache_state}\n\n"
            f"{bundled_state}\n\n"
            f"Last broadcast used: {last}\n\n"
            "Priority chain: live cache → bundled → synthetic."
        )
        await msg.reply(body, disable_notification=True)

    @r.message(Command("ether_refresh"))
    async def ether_refresh(msg: Message) -> None:
        """Drop the live cache so the next /ether refetches."""
        if not await require_admin(msg, rt.settings.admin_ids):
            return
        radio_fx.reset_live_cache()
        await msg.reply(
            "Live shortwave cache cleared. The next /ether will refetch "
            "from the WebSDR failover list (a few seconds slower than usual).",
            disable_notification=True,
        )

    async def _force_challenge(msg: Message, kind: str | None) -> None:
        if not await require_admin(msg, rt.settings.admin_ids):
            return
        who = display_name(msg.from_user) if msg.from_user else "anonymous"
        issued = await _issue_bef_challenge(
            rt, msg, who, from_action="bef", force_kind=kind,
        )
        if not issued:
            await msg.reply(
                "Failed to issue challenge (AI unavailable?).",
                disable_notification=True,
            )

    @r.message(Command("debug_challenge"))
    async def debug_challenge_random(msg: Message) -> None:
        await _force_challenge(msg, kind=None)

    @r.message(Command("debug_captcha"))
    async def debug_captcha(msg: Message) -> None:
        await _force_challenge(msg, kind="captcha")

    @r.message(Command("debug_trivia"))
    async def debug_trivia(msg: Message) -> None:
        await _force_challenge(msg, kind="trivia")

    @r.message(Command("debug_recipe"))
    async def debug_recipe(msg: Message) -> None:
        await _force_challenge(msg, kind="recipe")

    @r.message(Command("debug_clear_challenge"))
    async def debug_clear_challenge(msg: Message) -> None:
        if not await require_admin(msg, rt.settings.admin_ids):
            return
        parts = (msg.text or "").split()
        if len(parts) >= 2:
            try:
                target = int(parts[1])
            except ValueError:
                await msg.reply("Invalid chat id.", disable_notification=True)
                return
        else:
            target = msg.chat.id
        n = await rt.duckhunt.clear_all_bef_challenges(target)
        await msg.reply(
            f"Cleared {n} pending bef challenge(s) in chat {target}."
            if n else
            f"No pending bef challenges in chat {target}.",
            disable_notification=True,
        )

    @r.message(Command("debug_persona"))
    async def debug_persona(msg: Message) -> None:
        """Dump the exact persona prompt the bot would use for this chat.

        Resolution chain:
          1. chat_config.persona_custom (if set) — WINS over everything
          2. chat_config.persona == "neutral" → NEUTRAL_PROMPT
          3. chat_config.persona in {"dude", "pedro"} → /master_prompt
             override, or DEFAULT_DUDE_PROMPT if no override is set
          4. anything else → /master_prompt fallback
        Plus the dynamic ``persona_state`` (mood / word-of-day /
        stuck-word) appended as a second system message.
        """
        if not await require_admin(msg, rt.settings.admin_ids):
            return
        from ipedro.personas import (
            DEFAULT_DUDE_PROMPT, current_master_prompt, resolve_persona,
        )
        cfg = await rt.chats.get_config(msg.chat.id)
        if cfg is None:
            await msg.reply("No chat config — run /chat_config first.",
                            disable_notification=True)
            return
        resolved = resolve_persona(cfg.persona, cfg.persona_custom)
        state = await rt.persona_state.current(msg.chat.id)
        extra = rt.persona_state.to_system_prompt(state)
        master = current_master_prompt()
        master_overridden = master != DEFAULT_DUDE_PROMPT
        custom = (cfg.persona_custom or "").strip()
        # Which branch of resolve_persona fired?
        if custom:
            source = "chat_config.persona_custom (overrides /master_prompt)"
        elif (cfg.persona or "").lower() == "neutral":
            source = "built-in NEUTRAL_PROMPT (persona=neutral)"
        elif (cfg.persona or "dude").lower() in ("dude", "pedro"):
            source = ("/master_prompt override" if master_overridden
                      else "DEFAULT_DUDE_PROMPT (no override; /master_prompt reset)")
        else:
            source = (f"unknown persona '{cfg.persona}' → fell back to "
                      f"{'/master_prompt' if master_overridden else 'DEFAULT_DUDE_PROMPT'}")
        body = (
            f"📋 Persona resolution for chat {msg.chat.id}\n\n"
            f"chat_config.persona        = {cfg.persona!r}\n"
            f"chat_config.persona_custom = "
            f"{'<set, ' + str(len(custom)) + ' chars>' if custom else 'None'}\n"
            f"master_prompt override     = "
            f"{'yes (' + str(len(master)) + ' chars)' if master_overridden else 'no (using default)'}\n"
            f"persona_state extras       = "
            f"{'<set, ' + str(len(extra)) + ' chars>' if extra else 'None'}\n\n"
            f"Resolved persona source: {source}\n\n"
            f"--- system prompt actually sent (first 1500 chars) ---\n"
            f"{resolved[:1500]}"
            + (f"\n\n--- + dynamic extras ---\n{extra[:500]}" if extra else "")
        )
        await msg.reply(body[:4000], disable_notification=True)

    @r.message(Command("debug_duck"))
    async def debug_duck(msg: Message) -> None:
        if not await require_admin(msg, rt.settings.admin_ids):
            return
        if await rt.duckhunt.active_duck(msg.chat.id):
            await msg.reply("Duck already active.", disable_notification=True)
            return
        duck = await rt.duckhunt.spawn_duck(
            msg.chat.id, rt.settings.duckhunt_duck_lifetime_seconds,
        )
        await msg.reply(
            f"🦆 quack!\n[debug] duck id: {duck.id}",
            disable_notification=True,
        )

    return r
