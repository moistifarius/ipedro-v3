"""AI command handlers: /a /askai /aigen /aiedit /aivar /aitranslate /beneficiality /catfact."""

from __future__ import annotations

import io
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from ipedro.handlers.common import catify, fallback_cat_fact, get_or_create_chat_config
from ipedro.memory.context_builder import build_context
from ipedro.memory.summarizer import maybe_summarize
from ipedro.prompts import (
    BENEFICIALITY_PROMPT, CAT_FACT_PROMPT,
)
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)


def _strip_command(text: str | None) -> str:
    if not text:
        return ""
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) == 2 else ""


def build_router(rt: Runtime) -> Router:
    r = Router(name="ai")

    # /a and /askai - quick stateless AI answer (no memory write)
    @r.message(Command("a", "askai", "ask"))
    async def quick_ask(msg: Message) -> None:
        question = _strip_command(msg.text)
        if not question:
            await msg.reply(
                "Usage: /a <question>", disable_notification=True,
            )
            return
        await msg.bot.send_chat_action(msg.chat.id, "typing")
        answer = await rt.openai.short_completion(question, max_tokens=400)
        await msg.reply(answer or "(no response)", disable_notification=True)

    @r.message(Command("aigen", "generate"))
    async def aigen(msg: Message) -> None:
        prompt = _strip_command(msg.text)
        if not prompt:
            await msg.reply("Usage: /aigen <prompt>", disable_notification=True)
            return
        await msg.bot.send_chat_action(msg.chat.id, "upload_photo")
        data = await rt.openai.generate_image(prompt)
        if not data:
            await msg.reply("Image generation failed.", disable_notification=True)
            return
        await msg.reply_photo(
            BufferedInputFile(data, filename="aigen.png"),
            caption=prompt[:1000],
            disable_notification=True,
        )

    @r.message(Command("aiedit"))
    async def aiedit(msg: Message) -> None:
        await msg.reply(
            "Image editing requires an SDK-supported model and a mask. "
            "This command is preserved but not currently wired to a backend. "
            "Use /aigen for now.",
            disable_notification=True,
        )

    @r.message(Command("aivar"))
    async def aivar(msg: Message) -> None:
        await msg.reply(
            "Image variation requires an SDK-supported model. "
            "Preserved for compatibility; not currently wired. Use /aigen.",
            disable_notification=True,
        )

    @r.message(Command("aitranslate"))
    async def aitranslate(msg: Message) -> None:
        if not msg.reply_to_message or not msg.reply_to_message.voice:
            await msg.reply(
                "Reply to a voice note with /aitranslate.",
                disable_notification=True,
            )
            return
        voice = msg.reply_to_message.voice
        file = await msg.bot.get_file(voice.file_id)
        buf = io.BytesIO()
        await msg.bot.download_file(file.file_path, destination=buf)
        text = await rt.openai.translate_audio(buf, filename="voice.ogg")
        await msg.reply(text or "Translation failed.", disable_notification=True)

    @r.message(Command("catfact"))
    async def catfact(msg: Message) -> None:
        await msg.bot.send_chat_action(msg.chat.id, "typing")
        fact = await rt.openai.cheap_completion(CAT_FACT_PROMPT, max_tokens=120)
        await msg.reply(catify(fact or fallback_cat_fact()), disable_notification=True)

    @r.message(Command("beneficiality"))
    async def beneficiality(msg: Message) -> None:
        await get_or_create_chat_config(rt, msg)
        recent = await rt.memory.recent_messages(msg.chat.id, 10)
        if not recent:
            await msg.reply("Not enough context yet.", disable_notification=True)
            return
        conv = "\n".join(f"{m.role}: {m.content}" for m in recent)
        score = await rt.openai.cheap_completion(
            BENEFICIALITY_PROMPT.format(conversation=conv), max_tokens=10,
        )
        await msg.reply(
            f"Beneficiality score: {score or '?'}",
            disable_notification=True,
        )

    @r.message(Command("chat_config"))
    async def chat_config_cmd(msg: Message) -> None:
        """Show or update this chat's config (group admins / DM only)."""
        cfg = await get_or_create_chat_config(rt, msg)
        args = (msg.text or "").split()
        if len(args) < 2:
            await msg.reply(
                f"Response policy: {cfg.response_policy}\n"
                f"Ambient probability: {cfg.ambient_probability}\n"
                f"Persona: {cfg.persona}\n"
                f"Duckhunt enabled: {cfg.duckhunt_enabled}\n"
                f"Share-photo enabled: {cfg.share_photo_enabled}\n"
                f"Comic enabled: {cfg.comic_enabled}\n"
                f"Fortune enabled: {cfg.fortune_enabled}\n"
                f"Voice transcribe: {cfg.voice_transcribe}\n"
                f"Memory enabled: {cfg.memory_enabled}\n"
                f"Ether enabled: {cfg.ether_enabled}\n"
                f"Duck names public: {cfg.duck_names_public}\n"
                f"On this day: {cfg.on_this_day_enabled}\n"
                f"Monthly recap: {cfg.monthly_recap_enabled}\n\n"
                "Set a field: /chat_config <field> <value>\n"
                "  policy     commands|mention|reply|ambient|always\n"
                "  ambient    <0.0-1.0>\n"
                "  persona    dude|neutral|<free-form>\n"
                "  duckhunt   on|off\n"
                "  sharephoto on|off\n"
                "  comic      on|off\n"
                "  fortune    on|off\n"
                "  voice      on|off\n"
                "  memory     on|off\n"
                "  ether      on|off\n"
                "  ducknames  on|off — share this chat's named ducks in /ducknames\n"
                "  onthisday  on|off — daily 'on this day' nostalgia post",
                disable_notification=True,
            )
            return
        # Only the admin user (or the chat creator in a group) gets to edit.
        if not msg.from_user or msg.from_user.id not in rt.settings.admin_ids:
            # Allow chat owners to edit too (best-effort).
            try:
                member = await rt.bot.get_chat_member(msg.chat.id, msg.from_user.id)
                if member.status not in ("creator", "administrator"):
                    await msg.reply("Only chat admins can edit config.", disable_notification=True)
                    return
            except Exception:
                await msg.reply("Only chat admins can edit config.", disable_notification=True)
                return

        if len(args) < 3:
            await msg.reply("Usage: /chat_config <field> <value>", disable_notification=True)
            return
        field, raw = args[1].lower(), args[2]
        updates: dict = {}
        if field in ("policy", "response_policy"):
            if raw not in ("commands", "mention", "reply", "ambient", "always"):
                await msg.reply("Invalid policy.", disable_notification=True)
                return
            updates["response_policy"] = raw
        elif field == "ambient":
            try:
                updates["ambient_probability"] = max(0.0, min(1.0, float(raw)))
            except ValueError:
                await msg.reply("Invalid ambient probability.", disable_notification=True)
                return
        elif field == "persona":
            updates["persona"] = raw
            # Custom personas via the remaining argument tail.
            tail = (msg.text or "").split(None, 3)
            if raw not in ("dude", "pedro", "neutral") and len(tail) == 4:
                updates["persona_custom"] = tail[3]
            elif raw in ("dude", "pedro", "neutral"):
                updates["persona_custom"] = None
        elif field == "duckhunt":
            updates["duckhunt_enabled"] = raw.lower() in ("on", "true", "1", "yes")
        elif field in ("sharephoto", "share_photo"):
            updates["share_photo_enabled"] = raw.lower() in ("on", "true", "1", "yes")
        elif field == "comic":
            updates["comic_enabled"] = raw.lower() in ("on", "true", "1", "yes")
        elif field == "fortune":
            updates["fortune_enabled"] = raw.lower() in ("on", "true", "1", "yes")
        elif field == "voice":
            updates["voice_transcribe"] = raw.lower() in ("on", "true", "1", "yes")
        elif field == "memory":
            updates["memory_enabled"] = raw.lower() in ("on", "true", "1", "yes")
        elif field == "ether":
            updates["ether_enabled"] = raw.lower() in ("on", "true", "1", "yes")
        elif field in ("ducknames", "duck_names", "duck_names_public"):
            updates["duck_names_public"] = raw.lower() in ("on", "true", "1", "yes")
        elif field in ("onthisday", "on_this_day", "on_this_day_enabled"):
            updates["on_this_day_enabled"] = raw.lower() in ("on", "true", "1", "yes")
        elif field in ("monthlyrecap", "monthly_recap", "monthly_recap_enabled"):
            updates["monthly_recap_enabled"] = raw.lower() in ("on", "true", "1", "yes")
        else:
            await msg.reply("Unknown field.", disable_notification=True)
            return

        await rt.chats.update_config(msg.chat.id, **updates)
        await msg.reply("Updated.", disable_notification=True)

    return r
