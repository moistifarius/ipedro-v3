"""AI command handlers: /a /askai /aigen /aitranslate /beneficiality /catfact."""

from __future__ import annotations

import io
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from ipedro.handlers.common import catify, fallback_cat_fact, get_or_create_chat_config
from ipedro.memory.tokens import count_tokens
from ipedro.prompts import (
    BENEFICIALITY_PROMPT, CAT_FACT_PROMPT,
)
from ipedro.runtime import Runtime

log = logging.getLogger(__name__)

# Same cap as /master_prompt setfile (admin.py) — plenty for a persona
# prompt, small enough to never worry about memory while decoding it.
_PERSONA_FILE_MAX_BYTES = 64 * 1024


def _strip_command(text: str | None) -> str:
    if not text:
        return ""
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) == 2 else ""


async def _set_persona_from_file(rt: Runtime, msg: Message, name: str) -> None:
    """Set THIS chat's persona_custom from an attached/replied-to .txt file.

    Mirrors /master_prompt setfile (admin.py) — same size cap and UTF-8
    validation — but scoped to msg.chat.id instead of the global override,
    for persona prompts too long to fit in one Telegram message (4096
    chars) as inline /chat_config text."""
    doc = msg.document or (
        msg.reply_to_message.document if msg.reply_to_message else None
    )
    if doc is None:
        await msg.reply(
            "Send the new persona as a .txt file and either caption it "
            f"/chat_config persona {name} setfile or reply to the file "
            f"with /chat_config persona {name} setfile.",
            disable_notification=True,
        )
        return
    if doc.file_size and doc.file_size > _PERSONA_FILE_MAX_BYTES:
        await msg.reply(
            f"That file is {doc.file_size} bytes; cap is "
            f"{_PERSONA_FILE_MAX_BYTES}. Shorten the prompt.",
            disable_notification=True,
        )
        return
    try:
        file = await msg.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await msg.bot.download_file(file.file_path, destination=buf)
    except Exception as exc:
        log.warning("chat_config persona setfile download failed: %s", exc)
        await msg.reply(f"Couldn't download that file: {exc}",
                        disable_notification=True)
        return
    data = buf.getvalue()
    if len(data) > _PERSONA_FILE_MAX_BYTES:
        await msg.reply(
            f"File is {len(data)} bytes; cap is {_PERSONA_FILE_MAX_BYTES}.",
            disable_notification=True,
        )
        return
    try:
        new_text = data.decode("utf-8")
    except UnicodeDecodeError:
        await msg.reply(
            "File isn't valid UTF-8. Save it as plain UTF-8 text and "
            "try again.",
            disable_notification=True,
        )
        return
    new_text = new_text.strip()
    if not new_text:
        await msg.reply(
            "File is empty — refusing to clobber the persona.",
            disable_notification=True,
        )
        return
    await rt.chats.update_config(
        msg.chat.id, persona=name, persona_custom=new_text,
    )
    tokens = count_tokens(new_text)
    budget = rt.settings.context_max_tokens
    suffix = ""
    if tokens >= budget:
        suffix = (
            f"\n\n⚠️ {tokens} tokens exceeds context_max_tokens ({budget}); "
            "the persona will be dropped at runtime. Shorten the prompt."
        )
    elif tokens > budget * 0.75:
        suffix = (
            f"\n\n⚠️ {tokens} tokens uses >75% of the {budget}-token "
            "context budget; little room left for memory/history."
        )
    await msg.reply(
        f"Persona '{name}' updated for this chat ({len(new_text)} chars, "
        f"~{tokens} tokens).{suffix}",
        disable_notification=True,
    )


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
        answer = await rt.openai.short_completion(
            question, max_tokens=400, chat_id=msg.chat.id,
        )
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
        # Command may arrive as msg.text or as msg.caption (when a persona
        # .txt file is uploaded with the command as its caption).
        args = (msg.text or msg.caption or "").split()
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
                f"Monthly recap: {cfg.monthly_recap_enabled}\n"
                f"Automod: {cfg.automod_enabled}\n\n"
                "Set a field: /chat_config <field> <value>\n"
                "  policy     commands|mention|reply|ambient|always\n"
                "  ambient    <0.0-1.0>\n"
                "  persona    dude|pedro|neutral, or <name> <prompt text> for "
                "custom (or <name> setfile, replying to/captioning a .txt, "
                "for prompts over 4096 chars)\n"
                "  duckhunt   on|off\n"
                "  sharephoto on|off\n"
                "  comic      on|off\n"
                "  fortune    on|off\n"
                "  voice      on|off\n"
                "  memory     on|off\n"
                "  ether      on|off\n"
                "  ducknames  on|off — share this chat's named ducks in /ducknames\n"
                "  automod    on|off — copypasta/meme canned responses",
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
            # Custom personas via the remaining argument tail, or a .txt
            # file (setfile) for prompts too long for one Telegram message.
            tail = (msg.text or msg.caption or "").split(None, 3)
            if raw in ("dude", "pedro", "neutral"):
                updates["persona"] = raw
                updates["persona_custom"] = None
            elif len(tail) == 4 and tail[3].strip().lower() == "setfile":
                await _set_persona_from_file(rt, msg, name=raw)
                return
            elif len(tail) == 4:
                updates["persona"] = raw
                updates["persona_custom"] = tail[3]
            else:
                # Unknown key with no custom text would silently resolve
                # to the default prompt — refuse instead of lying "Updated."
                await msg.reply(
                    f"Unknown persona '{raw}'. Use dude, pedro, or neutral — "
                    "or supply the custom prompt text after the name:\n"
                    "/chat_config persona <name> <prompt text>",
                    disable_notification=True,
                )
                return
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
        elif field in ("monthlyrecap", "monthly_recap", "monthly_recap_enabled"):
            updates["monthly_recap_enabled"] = raw.lower() in ("on", "true", "1", "yes")
        elif field in ("automod", "automod_enabled"):
            updates["automod_enabled"] = raw.lower() in ("on", "true", "1", "yes")
        else:
            await msg.reply("Unknown field.", disable_notification=True)
            return

        await rt.chats.update_config(msg.chat.id, **updates)
        await msg.reply("Updated.", disable_notification=True)

    return r
