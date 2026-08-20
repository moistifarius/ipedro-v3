"""AutoModerator-style canned responses: no echoes, real copypastas, media.

House rule under test: the bot must always ADD something — a continuation,
punchline, retort, copypasta, or the actual meme media. It never just repeats
the trigger phrase back with an emoji.
"""

from __future__ import annotations

import random
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro.handlers.automod import (
    _ALL_YOUR_BASE, _AMONG_US_COPYPASTA, _AUTOMOD_TRIGGERS, _COPIUM_LINES,
    _GAY_COPYPASTA, _GNU_LINUX_PASTA, _HOLY_HELL_CHAIN, _JACKDAW_PASTA,
    _KYS_LINES, _L_RATIO_COPYPASTA, DaleGif, MediaResponse, _automod_response,
)


# ── copypastas ───────────────────────────────────────────────────────────────

def test_gay_returns_the_copypasta():
    assert _automod_response("that's so gay lol") == _GAY_COPYPASTA
    assert _GAY_COPYPASTA.startswith("Yeah can you imagine being gay lol?")


def test_gay_matches_standalone_word_only():
    for t in ("gay", "you're GAY", "so gay!", "gays", "a gay man"):
        assert _automod_response(t) == _GAY_COPYPASTA, t
    for t in ("gaymer", "gaylord", "gayer", "gayest", "margay"):
        assert _automod_response(t) != _GAY_COPYPASTA, t


def test_among_us():
    assert _automod_response("have you played among us").startswith(
        "Stop posting about Among Us!")
    assert _automod_response("amogus") == _AMONG_US_COPYPASTA
    assert _automod_response("that's kinda sussy") == _AMONG_US_COPYPASTA
    # bare 'sus' is too common to fire the wall of text
    assert _automod_response("that seems sus") is None
    assert _automod_response("suspicious activity") is None


def test_holy_hell_summons_the_google_en_passant_chain():
    assert _automod_response("holy hell") == _HOLY_HELL_CHAIN
    assert _automod_response("google en passant").startswith("Holy hell!")
    assert _automod_response("what the hell") != _HOLY_HELL_CHAIN


def test_l_ratio_fires_only_on_the_taunt_forms():
    assert _automod_response("L + ratio") == _L_RATIO_COPYPASTA
    assert _automod_response("you just got ratioed") == _L_RATIO_COPYPASTA
    assert _automod_response("get ratio'd") == _L_RATIO_COPYPASTA
    # bare 'ratio' is far too common (aspect ratio, gear ratio, math) to fire
    assert _automod_response("what's the aspect ratio") is None
    assert _automod_response("a 16:9 ratio") is None
    assert _automod_response("cool aspect ratio") is None


def test_long_pastas_fire_verbatim():
    assert _automod_response("all your base are belong to us") == _ALL_YOUR_BASE
    assert _automod_response("i use linux btw") == _GNU_LINUX_PASTA
    assert _automod_response("a jackdaw is a crow") == _JACKDAW_PASTA
    assert _JACKDAW_PASTA.startswith("Here's the thing.")


# ── kys deflection ───────────────────────────────────────────────────────────

def test_kys_gets_a_deflection_and_wins_priority():
    for t in ("kys", "kill yourself", "just neck yourself",
              "i want to kill myself", "killurself", "kill ur self"):
        assert _automod_response(t, random.Random(0)) in _KYS_LINES, t
    # kys intercepts first — a joke trigger in the same message can't win
    assert _automod_response("kys you gay loser", random.Random(0)) in _KYS_LINES
    # deflection register only — never an actual instruction to self-harm.
    banned = re.compile(r"\b(kill|kys|die|neck|rope)\b", re.IGNORECASE)
    for line in _KYS_LINES:
        assert not banned.search(line), line
    assert _automod_response("that joke killed me lol") not in _KYS_LINES


# ── the no-echo invariant ────────────────────────────────────────────────────
# For every trigger row: a canonical input a user would actually type, chosen
# so it reaches that row (first match wins). The response must never be just
# that phrase again. Adding a row without a sample fails the meta-test.

_SAMPLES: dict[str, str] = {
    r"\bkys\b|(kill|neck)\s*(your|my|ur|yr)\s*self": "kys",
    r"\bpocket\s*sand\b": "pocket sand",
    r"\bpropane\b": "propane",
    r"\bthat boy ain'?t right\b": "that boy ain't right",
    r"\bsh+-?sha+\b": "sh-sha",
    r"\bdeep state\b|\bfalse flag\b|\bchemtrails?\b|\bblack helicopters?\b|\btin\s*foil hat\b|\bnew world order\b|\bmen in black\b|\bgrassy knoll\b|\blizard people\b|\bsheeple\b|\barea 51\b|\bmoon landing\b": "deep state",
    r"\bthey'?re watching\b|\bwake up sheeple\b": "they're watching",
    r"\bsquirrel tactic\b": "squirrel tactic",
    r"\bgays?\b": "gay",
    r"\bamong\s*us\b|\bamogus\b|\bsussy\b": "among us",
    r"\bholy\s+hell\b|\ben\s+passant\b": "holy hell",
    r"\bl\s*\+\s*ratio\b|\bratio(?:ed|'?d)\b": "l + ratio",
    r"\ball your base\b": "all your base are belong to us",
    r"\blinux\b": "i use linux",
    r"\bjackdaw\b": "a jackdaw is a crow",
    r"\bsurprised pikachu\b": "surprised pikachu",
    r"\bstonks\b": "stonks",
    r"\bis this a pigeon\b": "is this a pigeon",
    r"\bsame picture\b": "these are the same picture",
    r"\bhonest work\b": "it ain't much but it's honest work",
    r"\bscientist myself\b": "i'm something of a scientist myself",
    r"\bx to doubt\b": "press x to doubt",
    r"\bit'?s wednesday\b": "it's wednesday",
    r"\byou died\b": "you died",
    r"\bgigachad\b": "gigachad",
    r"\bone does not simply\b": "one does not simply",
    r"\bnever gonna give you up\b": "never gonna give you up",
    r"\broad work ahead\b": "road work ahead",
    r"\bhasta la vista\b": "hasta la vista baby",
    r"\bza warudo\b": "za warudo",
    r"\bwhy are you running\b": "why are you running",
    r"\bbased\b": "based",
    r"\bsneed\b": "sneed",
    r"\btrans rights\b": "trans rights",
    r"\bnl\b": "nl",
    r"\bcopium\b": "copium",
    r"\bmorb(?:ius|in|ing)\b": "it's morbin time",
    r"\bskibidi\b": "skibidi toilet",
    r"\bohio\b": "only in ohio",
    r"\bsigma\b": "what the sigma",
    r"\brizz\b": "w rizz",
    r"\bwe live in a society\b": "we live in a society",
    r"\bmitochondria\b": "mitochondria",
    r"\breduced to atoms\b": "gone. reduced to atoms",
    r"\bnarwhal bacons\b": "the narwhal bacons",
    r"\bgyat+\b": "gyatt",
    r"\bfanum tax\b": "fanum tax",
    r"\blet (?:him|her|them) cook\b": "let him cook",
    r"\breddit moment\b": "reddit moment",
    r"\bdeez nuts\b": "deez nuts",
    r"\bok boomer\b": "ok boomer",
    r"\btask failed successfully\b": "task failed successfully",
    r"\btook that personally\b": "and i took that personally",
    r"\bmodern problems\b": "modern problems",
    r"\bfollow the damn train\b": "all we had to do was follow the damn train",
    r"\bhello there\b": "hello there",
    r"\bhigh ground\b": "i have the high ground",
    r"\bi love democracy\b": "i love democracy",
    r"\byou were the chosen one\b": "you were the chosen one",
    r"\bthis is where the fun begins\b": "this is where the fun begins",
    r"\bi am your father\b": "luke, i am your father",
    r"\bthere is no try\b": "there is no try",
    r"\black of faith\b": "i find your lack of faith disturbing",
    r"\bthese aren'?t the droids\b": "these aren't the droids you're looking for",
    r"\bthis is the way\b": "this is the way",
    r"\blive long and prosper\b": "live long and prosper",
    r"\bresistance is futile\b": "resistance is futile",
    r"\byou shall not pass\b": "you shall not pass",
    r"\band my axe\b": "and my axe",
    r"\bfly,?\s+you fools\b": "fly, you fools",
    r"\bhow the turntables\b": "how the turntables",
    r"\bthat'?s what she said\b": "that's what she said",
    r"\bbears\.?\s*beets\b": "bears. beets.",
    r"\bidentity theft\b": "identity theft",
    r"\bi want the truth\b": "i want the truth",
    r"\bbox of chocolates\b": "life is like a box of chocolates",
    r"\byou talkin['g]? to me\b": "you talkin to me",
    r"\bbigger boat\b": "we're gonna need a bigger boat",
    r"\boffer (?:he|you) can'?t refuse\b": "an offer he can't refuse",
    r"\bhouston,? we have a problem\b": "houston, we have a problem",
    r"\bto infinity\b": "to infinity",
    r"\bjust keep swimming\b": "just keep swimming",
    r"\bhakuna matata\b": "hakuna matata",
    r"\bwhy so serious\b": "why so serious",
    r"\bhandle the truth\b": "you can't handle the truth",
    r"\bmake fetch happen\b": "let's make fetch happen",
    r"\bget in loser\b": "get in loser",
    r"\bon wednesdays we wear pink\b": "on wednesdays we wear pink",
    r"\bthe limit does not exist\b": "the limit does not exist",
    r"\blook at all those chickens\b": "look at all those chickens",
    r"\bthey were roommates\b": "and they were roommates",
    r"\bwar never changes\b": "war never changes",
    r"\bwould you kindly\b": "would you kindly",
    r"\bfinally awake\b": "ah, you're finally awake",
    r"\barrow (?:in|to) the knee\b": "i took an arrow to the knee",
    r"\bpraise the sun\b": "praise the sun",
    r"\bfinish him\b": "finish him",
    r"\bfatality\b": "fatality",
    r"\bbarrel roll\b": "do a barrel roll",
    r"\bdangerous to go alone\b": "it's dangerous to go alone",
    r"\bobjection\b": "objection!",
    r"\badditional pylons\b": "you must construct additional pylons",
    r"\bleeroy\b": "leeroy jenkins",
    r"\bfor the horde\b": "for the horde",
    r"\banother castle\b": "our princess is in another castle",
    r"\bit'?s[- ]?a me\b": "it's-a me",
    r"\bsuper effective\b": "it's super effective",
    r"\bhadouken\b": "hadouken",
    r"\bget over here\b": "get over here",
    r"\bgit gud\b": "git gud",
    r"\bdysentery\b": "dysentery",
    r"\bpay respects\b": "press f to pay respects",
    r"\bover 9000\b": "it's over 9000",
    r"\bjojo reference\b": "is this a jojo reference",
    r"\bnothing personnel\b": "nothing personnel, kid",
    r"\bomae wa mou\b": "omae wa mou shindeiru",
    r"\bkamehameha\b": "kamehameha",
    r"\bora ora\b": "ora ora ora",
    r"\byare yare\b": "yare yare daze",
    r"\bkeikaku\b": "keikaku",
    r"\bdattebayo\b": "dattebayo",
    r"\bthe cake is a lie\b": "the cake is a lie",
    r"\bwake me up inside\b": "wake me up inside",
    r"\bsomebody once told me\b": "somebody once told me",
    r"\bogres are like onions\b": "ogres are like onions",
    r"\bthis is a wendy'?s\b": "sir, this is a wendy's",
    r"\bi also choose this guy\b": "i also choose this guy",
    r"(?<!\d)69(?!\d)": "69",
    r"(?<!\d)420(?!\d)": "420",
}


def _normalize(s: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in s.lower()).split())


def _variants(response) -> tuple[str, ...]:
    """Every string a response can put on screen, whatever its type."""
    if isinstance(response, MediaResponse):
        return (response.caption, response.fallback)
    if isinstance(response, DaleGif):
        return tuple(v for v in (response.caption, response.fallback) if v)
    if isinstance(response, str):
        return (response,)
    return tuple(response)


def test_every_trigger_has_a_sample():
    patterns = {p.pattern for p, _ in _AUTOMOD_TRIGGERS}
    missing = patterns - set(_SAMPLES)
    stale = set(_SAMPLES) - patterns
    assert not missing, f"add a _SAMPLES entry for: {missing}"
    assert not stale, f"remove stale _SAMPLES entries: {stale}"


def test_samples_reach_their_own_row():
    for pattern, response in _AUTOMOD_TRIGGERS:
        sample = _SAMPLES[pattern.pattern]
        got = _automod_response(sample, random.Random(0))
        if isinstance(response, (MediaResponse, DaleGif)):
            assert got == response, (sample, got)
        elif isinstance(response, str):
            assert got == response, (sample, got)
        else:
            assert got in response, (sample, got)


def test_no_response_is_an_echo():
    """The core rule: never repeat the trigger phrase back ± emoji."""
    for pattern, response in _AUTOMOD_TRIGGERS:
        sample = _normalize(_SAMPLES[pattern.pattern])
        for v in _variants(response):
            assert _normalize(v) != sample, (
                f"echo response for {pattern.pattern!r}: {v!r}")


# ── media entries ────────────────────────────────────────────────────────────

def _media_entries():
    return [(p, r) for p, r in _AUTOMOD_TRIGGERS if isinstance(r, MediaResponse)]


def test_media_entries_are_wellformed():
    entries = _media_entries()
    assert len(entries) >= 15   # 11 photos + 5 gifs pinned
    for _p, m in entries:
        assert m.kind in ("photo", "gif"), m
        assert m.url.startswith("https://"), m.url
        assert m.caption, m
        assert m.fallback and len(m.fallback) <= 4000, m


def test_media_hosts_are_pinned_stable_ones():
    ok_hosts = ("i.imgflip.com", "i.kym-cdn.com", "media.giphy.com",
                "media.tenor.com", "media1.tenor.com")
    for _p, m in _media_entries():
        host = m.url.split("/")[2]
        assert host in ok_hosts, m.url


def test_matcher_returns_media_untouched():
    got = _automod_response("stonks")
    assert isinstance(got, MediaResponse)
    assert got.kind == "photo"
    got = _automod_response("za warudo")
    assert isinstance(got, MediaResponse)
    assert got.kind == "gif"


# ── the media send path in chat.py ───────────────────────────────────────────

def _fake_msg():
    return SimpleNamespace(
        chat=SimpleNamespace(id=999),
        reply=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        reply_photo=AsyncMock(return_value=SimpleNamespace(message_id=2)),
        reply_animation=AsyncMock(return_value=SimpleNamespace(message_id=3)),
    )


_PHOTO = MediaResponse("photo", "https://i.imgflip.com/x.jpg", "cap", "fell back")
_GIF = MediaResponse("gif", "https://media.giphy.com/media/x/giphy.gif",
                     "cap", "fell back")


@pytest.mark.asyncio
async def test_media_photo_path_sends_photo(monkeypatch):
    from ipedro.handlers import chat
    monkeypatch.setattr(chat, "fetch_automod_media", AsyncMock(return_value=b"IMG"))
    msg = _fake_msg()
    await chat._reply_automod_media(msg, _PHOTO)
    msg.reply_photo.assert_awaited()
    assert msg.reply_photo.await_args.kwargs["caption"] == "cap"
    msg.reply_animation.assert_not_awaited()
    msg.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_media_gif_path_sends_animation(monkeypatch):
    from ipedro.handlers import chat
    monkeypatch.setattr(chat, "fetch_automod_media", AsyncMock(return_value=b"GIF"))
    msg = _fake_msg()
    await chat._reply_automod_media(msg, _GIF)
    msg.reply_animation.assert_awaited()
    msg.reply_photo.assert_not_awaited()
    msg.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_media_fetch_failure_falls_back_to_text(monkeypatch):
    from ipedro.handlers import chat
    monkeypatch.setattr(chat, "fetch_automod_media", AsyncMock(return_value=None))
    msg = _fake_msg()
    await chat._reply_automod_media(msg, _PHOTO)
    msg.reply_photo.assert_not_awaited()
    msg.reply.assert_awaited_with("fell back", disable_notification=True)


@pytest.mark.asyncio
async def test_media_send_failure_falls_back_to_text(monkeypatch):
    from ipedro.handlers import chat
    monkeypatch.setattr(chat, "fetch_automod_media", AsyncMock(return_value=b"IMG"))
    msg = _fake_msg()
    msg.reply_photo = AsyncMock(side_effect=RuntimeError("telegram said no"))
    await chat._reply_automod_media(msg, _PHOTO)
    msg.reply.assert_awaited_with("fell back", disable_notification=True)


# ── the per-chat kill switch (chat.py gate) ──────────────────────────────────

@pytest.mark.asyncio
async def test_automod_disabled_chat_gets_no_canned_reply():
    from ipedro.handlers.chat import build_router
    from tests.test_captcha_intercept import _msg, _rt_with

    rt = _rt_with()
    kenobi = "General Kenobi! You are a bold one. ⚔️"

    rt.chats.get_config.return_value.automod_enabled = True
    router = build_router(rt)
    handler = next(h.callback for h in router.observers["message"].handlers
                   if h.callback.__name__ == "on_message")
    msg = _msg(text="hello there")
    await handler(msg)
    assert any(c.args[0] == kenobi for c in msg.reply.await_args_list)

    # Disabled: same trigger text produces no canned reply. (Policy 'mention'
    # so the fall-through stops at should_respond instead of the AI pipeline,
    # which this stub doesn't model — automod itself fires under 'mention'.)
    cfg = rt.chats.get_config.return_value
    cfg.automod_enabled = False
    cfg.response_policy = "mention"
    msg2 = _msg(text="hello there")
    await handler(msg2)
    msg2.reply.assert_not_awaited()

    # sanity: under 'mention' policy the canned reply DOES fire when enabled
    cfg.automod_enabled = True
    msg3 = _msg(text="hello there")
    await handler(msg3)
    assert any(c.args[0] == kenobi for c in msg3.reply.await_args_list)


# ── boundaries & misc ────────────────────────────────────────────────────────

def test_word_boundaries_hold():
    for t in ("the stigma around it", "morbid curiosity", "ohioan pride",
              "rizzling up", "copiumm", "only kidding", "channel surfing",
              "i objected to the plan", "come over here now",
              "in modern society", "what a fatal error", "gaymer"):
        assert _automod_response(t) is None, t


def test_number_jokes_need_boundaries():
    assert _automod_response("nice 69 haha") == "nice"
    assert _automod_response("420 baby") == "blaze it 🔥"
    assert _automod_response("born in 1969") is None
    assert _automod_response("that costs 4200") is None


def test_deleted_echo_triggers_are_gone():
    for t in ("gotta go fast", "my name is jeff", "show me the money",
              "plus ultra", "bazinga", "what are those",
              "flawless victory"):
        got = _automod_response(t)
        assert got is None, (t, got)


def test_tuple_branch_draws_and_varies():
    for seed in range(20):
        assert _automod_response("massive copium", random.Random(seed)) in _COPIUM_LINES
    seen = {_automod_response("copium", random.Random(s)) for s in range(30)}
    assert len(seen) > 1


def test_every_text_response_fits_the_telegram_message_limit():
    for _pattern, response in _AUTOMOD_TRIGGERS:
        for v in _variants(response):
            assert isinstance(v, str) and 0 < len(v) <= 4000, v[:60]


def test_no_trigger_returns_none():
    assert _automod_response("just a normal message") is None
    assert _automod_response("") is None
    assert _automod_response(None) is None


def test_table_shape_is_extensible():
    for pattern, response in _AUTOMOD_TRIGGERS:
        assert hasattr(pattern, "search")
        assert isinstance(response, (str, tuple, MediaResponse, DaleGif))


# ── Dale GIF rows ────────────────────────────────────────────────────────────

def _dale_rows():
    return [(p, r) for p, r in _AUTOMOD_TRIGGERS if isinstance(r, DaleGif)]


def test_no_dale_trigger_matches_the_bots_own_name():
    """The load-bearing guard.

    This table intercepts and RETURNS, before the AI ever runs. A Dale row
    matching the bot's own name would mean that addressing it by name gets a
    GIF instead of an answer — it would simply stop replying when spoken to.
    """
    addressed = [
        "dale", "dale gribble", "rusty shackleford", "idale", "boomhauer",
        "pedro", "the dude", "duderino", "hey dale", "dale what do you think",
        "thanks dale", "rusty, you there?",
    ]
    for pattern, response in _dale_rows():
        for text in addressed:
            assert not pattern.search(text), (
                f"Dale row {pattern.pattern!r} swallows {text!r} — the bot "
                f"would answer its own name with a GIF ({response.tag})")


def test_dale_rows_are_wellformed():
    rows = _dale_rows()
    assert len(rows) >= 8
    for _pattern, r in rows:
        assert r.tag and r.tag == r.tag.lower(), r
        assert r.fallback, r          # never degrade into silence
        assert len(r.fallback) <= 4000


def test_dale_tags_all_exist_in_the_seeded_library():
    """A typo'd tag would silently fall back to 'any Dale GIF' forever."""
    from ipedro.dale_gifs import _SEED_GIFS
    seeded = {t for _url, tags in _SEED_GIFS for t in tags}
    for _pattern, r in _dale_rows():
        assert r.tag in seeded, f"tag {r.tag!r} is in no seeded GIF"


def test_dale_triggers_need_their_distinctive_phrase():
    """Bare conversational words must not fire — the reply would replace a
    real answer to an ordinary question."""
    for text in ("the government said so", "that's a conspiracy",
                 "aliens are cool", "call the fbi", "surveillance state",
                 "i saw a black cat", "the moon is bright"):
        got = _automod_response(text)
        assert not isinstance(got, DaleGif), (text, got)


def test_converted_rows_keep_their_old_text_as_the_fallback():
    reddit = _automod_response("reddit moment")
    assert isinstance(reddit, DaleGif) and "ackshually" in reddit.fallback
    society = _automod_response("we live in a society")
    assert isinstance(society, DaleGif) and "gamers" in society.fallback
