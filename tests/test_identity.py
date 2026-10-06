"""Who the bot is, as configuration rather than hardcoding.

The same code has to be able to run as a bot that isn't Dale: answer to
its own names, take "bad <its name>" rebukes, and never reach for Dale's
catchphrases, his GIF library or his /start blurb. The tests pin both
halves — a configured bot is itself, and an unconfigured deployment is
still exactly Dale.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import addressed, identity, personas
from ipedro import dale_gifs as dale
from ipedro.capabilities import _REFLEXES, capability_brief
from ipedro.handlers import automod, automod_bits, basics, chat
from ipedro.memory.context_builder import BuiltContext
from ipedro.user_flags import is_insult_to_bot
from tests.test_addressed import _mention_rt
from tests.test_captcha_intercept import _msg, _rt_with

HANK_SETTINGS = dict(
    bot_name="Hank", bot_aliases="hank, hank hill", bot_flavor="plain",
)
HANK = identity.from_settings(SimpleNamespace(**HANK_SETTINGS))


def _as_hank(rt):
    """Make a test runtime a Hank deployment. Before build_router: the
    identity is read once, when the handlers are built."""
    for key, value in HANK_SETTINGS.items():
        setattr(rt.settings, key, value)
    return rt


def _handler(rt, name="on_message"):
    router = chat.build_router(rt)
    return next(h for h in router.observers["message"].handlers
                if h.callback.__name__ == name)


def _sent_texts(*msgs) -> list[str]:
    """Everything the handler said back on these messages."""
    out = []
    for m in msgs:
        for call in m.reply.await_args_list + m.answer.await_args_list:
            if call.args:
                out.append(call.args[0])
    return out


# ── building an identity ─────────────────────────────────────────────────────

def test_nothing_configured_is_dale_himself():
    """A test stub, or a deployment that predates any of this, is Dale —
    the very same object, not a lookalike built from his name."""
    assert identity.from_settings(SimpleNamespace()) is identity.DALE
    assert identity.from_settings(SimpleNamespace(
        bot_name="Dale", bot_aliases="", bot_flavor="dale",
    )) is identity.DALE


def test_the_real_settings_default_to_dale(monkeypatch):
    for var in ("BOT_NAME", "BOT_ALIASES", "BOT_FLAVOR", "BOT_PERSONA"):
        monkeypatch.delenv(var, raising=False)
    from ipedro.config import Settings
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert identity.from_settings(s) is identity.DALE
    assert s.bot_persona is None


def test_the_starting_persona_doesnt_collide_with_the_chat_persona_key(monkeypatch):
    """`default_persona` already existed — the per-chat persona KEY, "dude".
    Reusing the name for the starting persona's TEXT meant the later
    definition won, and Dale would have started with the prompt "dude"."""
    monkeypatch.setenv("BOT_PERSONA", "You are Hank Hill.")
    from ipedro.config import Settings
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.bot_persona == "You are Hank Hill."
    assert s.default_persona == "dude"


def test_a_configured_bot_from_the_environment(monkeypatch):
    monkeypatch.setenv("BOT_NAME", "Hank")
    monkeypatch.setenv("BOT_ALIASES", "hank, hank hill")
    monkeypatch.setenv("BOT_FLAVOR", "plain")
    from ipedro.config import Settings
    ident = identity.from_settings(Settings(_env_file=None))  # type: ignore[call-arg]
    assert ident.name == "Hank"
    assert ident.dale_flavor is False
    assert ident.name_re.search("hank hill, settle this")


def test_aliases_are_trimmed_lowercased_and_deduplicated():
    ident = identity.from_settings(SimpleNamespace(
        bot_name="Hank", bot_aliases=" Hank, hank ,HANK HILL,, ",
        bot_flavor="plain",
    ))
    assert ident.primary_alias == "hank"
    assert ident.rebuke_phrases == {"bad bot", "bad hank", "bad hank hill"}


def test_no_aliases_means_the_name_itself():
    ident = identity.from_settings(SimpleNamespace(
        bot_name="Hank", bot_aliases="", bot_flavor="plain",
    ))
    assert ident.primary_alias == "hank"
    assert ident.name_re.search("Hank?")
    assert ident.rebuke_phrases == {"bad bot", "bad hank"}


@pytest.mark.parametrize("text", [
    "hank what do you think", "ask Hank", "HANK HILL!", "hank   hill says",
    "yo hank.",
])
def test_a_configured_bot_knows_its_own_names(text):
    assert chat._mentions_pedro(text, HANK)


@pytest.mark.parametrize("text", [
    "dale what do you think", "rusty?", "pedro", "hankering for tacos",
    "shank", "thanks man",
])
def test_and_only_its_own(text):
    assert not chat._mentions_pedro(text, HANK)


def test_bot_still_means_any_bot():
    assert chat._mentions_pedro("shut up bot", HANK)


def test_an_alias_ending_in_punctuation_still_matches():
    """\\b needs a word character on the far side, so a plain \\b-wrapped
    'mr. t' could never match at the end of a message."""
    ident = identity.from_settings(SimpleNamespace(
        bot_name="T", bot_aliases="mr. t", bot_flavor="plain",
    ))
    assert ident.name_re.search("ask mr. t")
    assert ident.name_re.search("Mr.  T, settle this")
    assert not ident.name_re.search("mr. tee")


@pytest.mark.parametrize("text", [
    "dale", "dale gribble", "rusty", "rusty shackleford", "shackleford",
    "idale", "boomhauer", "boomhaur", "the dude", "duder", "duderino",
    "el duderino", "his dudeness", "pedro",
])
def test_dale_answers_to_every_name_he_always_has(text):
    assert chat._mentions_pedro(f"hey {text} you there")
    assert identity.DALE.name_re.search(text)


# ── what reaches the handlers ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_configured_bot_answers_when_called_by_its_name(monkeypatch):
    rt = _as_hank(_mention_rt(monkeypatch))
    msg = _msg(text="hank what do you reckon about this")
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    await _handler(rt).callback(msg)
    rt.openai.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_dales_name_doesnt_summon_another_bot(monkeypatch):
    rt = _as_hank(_mention_rt(monkeypatch))
    await _handler(rt).callback(_msg(text="dale what do you reckon about this"))
    rt.openai.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_classifier_is_told_the_bots_own_name():
    """The yes/no judge decides whether a message is for "{bot}" — and
    labels the bot's own past lines with that name. Asking it about Dale
    in Hank's chat would be asking about someone who isn't there."""
    addressed.note_bot_reply(42)        # in the conversation: ambiguous → model
    rt = SimpleNamespace(
        openai=SimpleNamespace(cheap_completion=AsyncMock(return_value="NO")),
        memory=SimpleNamespace(recent_messages=AsyncMock(return_value=[
            SimpleNamespace(role="assistant", content="propane's fine",
                            author_name=None),
        ])),
    )
    await addressed.wants_reply(
        rt, 42, speaker="Luke", text="the kitchen tap is dripping",
        memory_enabled=True, bot_name="Hank",
    )
    prompt = rt.openai.cheap_completion.await_args.args[0]
    assert "a member named Hank" in prompt
    assert "[Hank]: propane's fine" in prompt
    assert "Dale" not in prompt


@pytest.mark.asyncio
async def test_the_chat_handler_hands_the_classifier_its_name(monkeypatch):
    rt = _as_hank(_mention_rt(monkeypatch))
    seen = AsyncMock(return_value=False)
    monkeypatch.setattr(addressed, "wants_reply", seen)
    await _handler(rt).callback(_msg(text="the kitchen tap is dripping"))
    assert seen.await_args.kwargs["bot_name"] == "Hank"


@pytest.mark.asyncio
@pytest.mark.parametrize("text,dale_deletes,hank_deletes", [
    ("bad bot", True, True),
    ("bad dale", True, False),
    ("bad rusty", True, False),
    ("Bad Hank", False, True),
    ("bad hank hill", False, True),
])
async def test_rebukes_follow_the_bots_own_names(text, dale_deletes, hank_deletes):
    dale_filter = _handler(_rt_with(), "remove_message")
    hank_filter = _handler(_as_hank(_rt_with()), "remove_message")
    assert (await dale_filter.check(_msg(text=text)))[0] is dale_deletes
    assert (await hank_filter.check(_msg(text=text)))[0] is hank_deletes


@pytest.mark.asyncio
async def test_thanking_another_bot_gets_no_dale_catchphrase(monkeypatch):
    """'thanks dale' gets one of Dale's canned send-offs ('rusty
    shackleford. at your service'). Another bot thanked by name answers
    for itself instead, and Dale's names thank no one."""
    rt = _as_hank(_mention_rt(monkeypatch))
    handler = _handler(rt).callback
    wrong_name = _msg(text="thanks dale")
    by_name = _msg(text="thanks hank that helps")
    by_name.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    # Wrong name first: after a reply to this user, their next line is
    # his turn whatever it says.
    await handler(wrong_name)
    await handler(by_name)
    rt.openai.chat.assert_awaited_once()          # the AI answered "thanks hank"
    assert not set(_sent_texts(by_name, wrong_name)) & set(chat._THANKS_PEDRO_LINES)


@pytest.fixture
def credit_roll(monkeypatch):
    monkeypatch.setattr(chat, "_CREDIT_PROBABILITY", 1.0)
    monkeypatch.setattr(chat, "should_respond", lambda *a, **k: False)


@pytest.mark.asyncio
async def test_only_dale_takes_credit_in_his_own_words(credit_roll):
    dale_msg = _msg(text="that plan is great honestly")
    dale_msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    await _handler(_rt_with()).callback(dale_msg)
    assert _sent_texts(dale_msg)[0] in chat._CREDIT_LINES

    hank_msg = _msg(text="that plan is great honestly")
    await _handler(_as_hank(_rt_with())).callback(hank_msg)
    assert _sent_texts(hank_msg) == []


@pytest.fixture
def gif_sender(monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(dale, "send_random", sent)
    monkeypatch.setattr(chat, "should_respond", lambda *a, **k: False)
    return sent


def _group_rt():
    """A group chat on the default policy: where the ambient bits live."""
    rt = _rt_with()
    rt.chats.get_config.return_value.response_policy = "mention"
    return rt


@pytest.mark.asyncio
async def test_another_bot_never_rolls_for_an_ambient_dale_gif(gif_sender, monkeypatch):
    monkeypatch.setattr(chat, "_DALE_GIF_PROBABILITY", 1.0)
    await _handler(_as_hank(_group_rt())).callback(_msg(text="anyway the tap drips"))
    gif_sender.assert_not_awaited()
    await _handler(_group_rt()).callback(_msg(text="anyway the tap drips"))
    gif_sender.assert_awaited_once()              # control: Dale still does


@pytest.mark.asyncio
async def test_another_bot_skips_the_dale_gif_triggers(gif_sender):
    await _handler(_as_hank(_group_rt())).callback(_msg(text="pocket sand!"))
    gif_sender.assert_not_awaited()
    await _handler(_group_rt()).callback(_msg(text="pocket sand!"))
    gif_sender.assert_awaited_once()              # control: Dale still does


def test_skipping_dale_gifs_lets_a_later_trigger_answer():
    """First match wins, so 'pocket sand' would shadow 'stonks' for Dale.
    With the Dale rows passed over, the scan carries on to the meme."""
    assert isinstance(automod._automod_response("pocket sand"), automod.DaleGif)
    assert automod._automod_response("pocket sand", dale_gifs=False) is None
    assert (
        automod._automod_response("pocket sand and stonks", dale_gifs=False)
        == automod_bits._M_STONKS
    )
    assert automod._automod_response("based", dale_gifs=False) == (
        "Based? Based on what?"
    )


def test_no_dale_gif_ever_comes_back_with_them_off(monkeypatch):
    """The mechanism, on a table where every row matches everything — so
    it holds for whatever rows get added later, not just today's."""
    import re
    monkeypatch.setattr(automod, "_AUTOMOD_TRIGGERS", (
        (re.compile(""), automod.DaleGif("a")),
        (re.compile(""), automod.DaleGif("b")),
        (re.compile(""), "the first row that isn't a Dale GIF"),
    ))
    assert automod._automod_response("anything", dale_gifs=False) == (
        "the first row that isn't a Dale GIF"
    )
    assert automod._automod_response("anything") == automod.DaleGif("a")


# ── what the bot is told about itself ────────────────────────────────────────

def test_only_dale_is_told_he_has_gifs_of_himself():
    dale_brief = capability_brief()
    hank_brief = capability_brief(dale_flavor=False)
    assert "GIF of yourself" in dale_brief
    assert "GIF of yourself" not in hank_brief
    assert "stock line" in hank_brief
    # Nothing else differs.
    assert (
        dale_brief.replace(_REFLEXES[True], "")
        == hank_brief.replace(_REFLEXES[False], "")
    )


def test_dales_brief_is_unchanged_so_his_prompt_cache_survives():
    """The brief sits in the cached prefix; changing a byte of it for Dale
    would rewrite every chat's cache on deploy for nothing."""
    assert (
        "- Read everything said here and reply in text when spoken to.\n"
        "- React to messages with emoji, and occasionally post a GIF of "
        "yourself or a stock line on reflex. Those are yours; own them.\n"
        "- See what people post:"
    ) in capability_brief()


@pytest.mark.asyncio
async def test_the_chat_handler_briefs_another_bot_as_itself(monkeypatch):
    rt = _as_hank(_mention_rt(monkeypatch))
    briefs = []

    async def fake_build(**kwargs):
        briefs.append(kwargs["capabilities"])
        return BuiltContext(messages=[{"role": "user", "content": "x"}], tokens=1)

    monkeypatch.setattr(chat, "build_context", fake_build)
    msg = _msg(text="hank you there")
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    await _handler(rt).callback(msg)
    assert briefs and "GIF of yourself" not in briefs[0]


# ── insults ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "dale you're useless",              # never registered before: wrong names
    "rusty is so dumb",
    "shut up rusty shackleford",
    "dude you're garbage",              # the legacy names still count
    "pedro is trash",
    "useless bot",
])
def test_insulting_dale_by_any_of_his_names_registers(text):
    assert is_insult_to_bot(text)


@pytest.mark.parametrize("text", [
    "i love dale", "that was stupid of me", "the dale cooper reveal",
])
def test_and_plain_talk_doesnt(text):
    assert not is_insult_to_bot(text)


def test_insults_follow_the_configured_names():
    assert is_insult_to_bot("hank you're useless", HANK.names_pattern)
    assert is_insult_to_bot("useless bot", HANK.names_pattern)
    assert not is_insult_to_bot("dale you're useless", HANK.names_pattern)


@pytest.mark.asyncio
async def test_the_chat_handler_checks_insults_against_its_own_names(monkeypatch):
    rt = _as_hank(_mention_rt(monkeypatch))
    grudge = AsyncMock(return_value=False)
    monkeypatch.setattr(chat, "maybe_auto_grudge", grudge)
    await _handler(rt).callback(_msg(text="the kitchen tap is dripping"))
    assert grudge.await_args.kwargs["names_pattern"] == HANK.names_pattern


# ── how it introduces itself ─────────────────────────────────────────────────

def test_dales_help_is_word_for_word_what_it_was():
    assert basics.help_text_public() == basics.HELP_TEXT_PUBLIC
    assert basics.HELP_TEXT_PUBLIC.startswith("Sh-sha. Name's Dale.")
    assert "'bad dale'" in basics.HELP_TEXT_PUBLIC


def test_another_bots_help_is_in_its_own_name():
    text = basics.help_text_public(HANK)
    assert text.startswith("Hank here. I chat,")
    assert "'bad hank'" in text
    assert "'hank send that pic of X'" in text
    lowered = text.lower()
    for word in ("dale", "rusty", "shackleford", "sh-sha"):
        assert word not in lowered, word


def test_start_text():
    assert "Rusty Shackleford" in basics.start_text()
    assert basics.start_text(HANK) == "Hank here. Type /help for commands."


@pytest.mark.asyncio
async def test_start_and_help_reply_as_the_configured_bot():
    rt = _as_hank(_rt_with())
    rt.command_log = SimpleNamespace(add=AsyncMock())
    router = basics.build_router(rt)
    by_name = {h.callback.__name__: h.callback
               for h in router.observers["message"].handlers}
    start, help_ = _msg(text="/start"), _msg(text="/help")
    await by_name["start"](start)
    await by_name["help_"](help_)
    assert _sent_texts(start) == ["Hank here. Type /help for commands."]
    assert _sent_texts(help_)[0].startswith("Hank here.")


# ── the persona it starts as ─────────────────────────────────────────────────

@pytest.fixture
def fresh_personas(monkeypatch):
    monkeypatch.setattr(personas, "_default_prompt", personas.DEFAULT_DALE_PROMPT)
    monkeypatch.setattr(personas, "_master_prompt_override", None)


def test_the_starting_persona_replaces_dale(fresh_personas):
    personas.set_default_prompt("  You are Hank Hill.  ")
    assert personas.default_prompt() == "You are Hank Hill."
    assert personas.current_master_prompt() == "You are Hank Hill."
    assert personas.resolve_persona("dude", None) == "You are Hank Hill."


def test_master_prompt_still_overrides_it(fresh_personas):
    personas.set_default_prompt("You are Hank Hill.")
    personas.set_master_prompt_override("You are Bobby.")
    assert personas.current_master_prompt() == "You are Bobby."
    personas.set_master_prompt_override(None)          # /master_prompt reset
    assert personas.current_master_prompt() == "You are Hank Hill."


@pytest.mark.parametrize("unset", [None, "", "   "])
def test_no_starting_persona_means_dale(fresh_personas, unset):
    personas.set_default_prompt("You are Hank Hill.")
    personas.set_default_prompt(unset)
    assert personas.current_master_prompt() == personas.DEFAULT_DALE_PROMPT


# ── the generic word "bot" is not every bot's name ───────────────────────────

def _settings_for(**kw):
    return SimpleNamespace(bot_name="Hank", bot_aliases="hank, hank hill",
                           bot_flavor="plain", bot_persona=None, **kw)


def test_only_the_bot_that_runs_the_others_answers_to_the_word_bot():
    """With Dale and Hank in one group, "bot, settle this" would otherwise
    be answered by both, and "shut up bot" would earn the speaker a grudge
    in each bot's own database."""
    dale = identity.from_settings(SimpleNamespace())          # nothing set: Dale
    hank_child = identity.from_settings(_settings_for(manages_bots=False))
    hank_alone = identity.from_settings(_settings_for(manages_bots=True))
    assert dale is identity.DALE and dale.answers_to_bot_word
    assert hank_alone.answers_to_bot_word              # the only bot: "bot" is it
    assert not hank_child.answers_to_bot_word
    assert "bad bot" in dale.rebuke_phrases and "bad bot" in hank_alone.rebuke_phrases
    assert "bad bot" not in hank_child.rebuke_phrases
    assert "bad hank" in hank_child.rebuke_phrases     # its own name still works


def test_a_child_bot_is_addressed_by_its_names_only():
    hank = identity.from_settings(_settings_for(manages_bots=False))
    dale = identity.DALE
    for line in ("bot which one of you is right", "the chatbot is broken", "ok robot"):
        assert chat._mentions_pedro(line, dale), line
        assert not chat._mentions_pedro(line, hank), line
    assert chat._mentions_pedro("hank which one of you is right", hank)
    assert chat._mentions_pedro("what do you think hank hill", hank)


def test_a_child_bot_holds_no_grudge_for_an_insult_to_bots_in_general():
    from ipedro.user_flags import is_insult_to_bot

    hank = identity.from_settings(_settings_for(manages_bots=False))
    assert is_insult_to_bot("shut up bot", identity.DALE.names_pattern)
    assert not is_insult_to_bot("shut up bot", hank.names_pattern, bot_word=False)
    assert not is_insult_to_bot("you stupid bot", hank.names_pattern, bot_word=False)
    assert is_insult_to_bot("shut up hank", hank.names_pattern, bot_word=False)


def test_the_bot_noun_only_opens_the_classifier_for_the_bot_that_owns_it():
    from ipedro.addressed import quick_verdict

    line = "honestly the bot gets this wrong"
    assert quick_verdict(line, in_conversation=False) is None          # worth a look
    assert quick_verdict(line, in_conversation=False, bot_word=False) is False


# ── what a bot offers depends on whether it runs the others ──────────────────

def _help_msg(chat_type, user_id=7):
    return SimpleNamespace(
        chat=SimpleNamespace(id=-5 if chat_type != "private" else user_id, type=chat_type, title="t"),
        from_user=SimpleNamespace(id=user_id, is_bot=False, username="a", first_name="A", last_name=None),
        text="/help", reply=AsyncMock(),
    )


def _help_rt(*, manages_bots=True):
    from ipedro.config import Settings

    settings = Settings(telegram_bot_token="t", openai_api_key="k",   # type: ignore[call-arg]
                        database_url="postgresql://t/t", admin_user_ids="7",
                        manages_bots=manages_bots)
    return SimpleNamespace(
        settings=settings,
        chats=SimpleNamespace(upsert_chat=AsyncMock(), get_config=AsyncMock(return_value=None),
                              upsert_default_config=AsyncMock(
                                  return_value=SimpleNamespace(memory_enabled=True))),
        users=SimpleNamespace(upsert_user=AsyncMock()),
        command_log=SimpleNamespace(add=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_the_admin_help_is_only_ever_posted_in_a_private_chat():
    """It is headed "DM only"; in a group it told everyone which admin
    commands exist."""
    rt = _help_rt()
    handler = next(h.callback for h in basics.build_router(rt).observers["message"].handlers
                   if h.callback.__name__ == "help_")
    group, dm = _help_msg("supergroup"), _help_msg("private")
    await handler(group)
    await handler(dm)
    assert group.reply.await_count == 1                       # the public help only
    assert dm.reply.await_count == 2
    assert "Bot admin (DM only)" in dm.reply.await_args.args[0]
    assert "/evolve" in dm.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_a_bot_the_manager_runs_does_not_advertise_the_managers_commands():
    rt = _help_rt(manages_bots=False)
    handler = next(h.callback for h in basics.build_router(rt).observers["message"].handlers
                   if h.callback.__name__ == "help_")
    dm = _help_msg("private")
    await handler(dm)
    assert "/newbot" not in dm.reply.await_args.args[0]
    assert "/evolve" not in dm.reply.await_args.args[0]
