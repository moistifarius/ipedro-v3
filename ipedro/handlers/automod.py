"""AutoModerator-style canned responses — real r/shitposting bits & copypastas.

Keyword -> canned reply, in the spirit of the r/shitposting AutoModerator.
First match wins; this is consulted before the normal AI reply. Each response
is one of:
  - a single string,
  - a tuple of strings (one picked at random),
  - a MediaResponse (the actual meme image/GIF, fetched by URL and sent as
    a photo/animation; falls back to `fallback` text if the fetch or the
    send fails).

`_AUTOMOD_TRIGGERS` IS THE WHOLE EXTENSION POINT — add a (regex, response) row.

House rules (enforced by tests/test_automod.py):
- NO ECHOES. The bot never just repeats the trigger phrase back with an
  emoji — every response is a continuation, punchline, retort, copypasta,
  or the actual meme media. If a bit has no good non-echo response, it
  doesn't get a row.
- Every pattern is anchored with word boundaries / lookarounds so common
  words don't trip a wall of text (e.g. 'ratio' vs 'aspect ratio').
- Patterns use only simple alternations and bounded quantifiers — no nested
  quantifiers — so there is no catastrophic-backtracking (ReDoS) risk.
- The one-time serious case ('kys') gets a deflection that never instructs
  self-harm. See `_KYS_LINES`.
- Responses are static constants: no user input is ever interpolated.
- Media URLs are pinned to stable hosts (imgflip templates, KYM entry icons,
  giphy/tenor media) and were content-verified when added. A dead URL only
  costs the image: the text fallback still fires.
"""

from __future__ import annotations

import logging
import random
import re
from typing import NamedTuple

import httpx

log = logging.getLogger(__name__)


class MediaResponse(NamedTuple):
    """An automod reply that is an actual meme image ('photo') or GIF ('gif')."""

    kind: str          # "photo" | "gif"
    url: str           # pinned direct media URL (https)
    caption: str       # sent with the media; also the tracked snippet
    fallback: str      # text reply used when fetching/sending the media fails


# In-process cache of fetched media bytes. The URL set is small and fixed
# (~16 templates, ~10MB total worst case), so a plain dict is plenty.
_MEDIA_CACHE: dict[str, bytes] = {}
_MEDIA_TIMEOUT = 10.0
_MEDIA_MAX_BYTES = 10_000_000
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


async def fetch_automod_media(media: MediaResponse) -> bytes | None:
    """Download (and cache) the bytes for a MediaResponse. None on any failure."""
    cached = _MEDIA_CACHE.get(media.url)
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=_MEDIA_TIMEOUT,
            headers={"User-Agent": _UA},
        ) as client:
            async with client.stream("GET", media.url) as resp:
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MEDIA_MAX_BYTES:
                        log.info("automod media too large: %s", media.url)
                        return None
                    chunks.append(chunk)
        data = b"".join(chunks)
        if data:
            _MEDIA_CACHE[media.url] = data
            return data
    except Exception as exc:
        log.info("automod media fetch failed %s: %s", media.url, exc)
    return None


# ── 'gay' → a fixed copypasta bit. Matches the standalone word only. ──────────
_GAY_RE = re.compile(r"\bgays?\b", re.IGNORECASE)
_GAY_COPYPASTA = (
    "Yeah can you imagine being gay lol? Like seriously honest to god wanting "
    "to kiss boys. Putting your lips on another dude’s lips unironically. "
    "Holding his face in your hands to feel his skin on yours just for the "
    "comfort of knowing he’s there. Looking into his eyes and realizing for "
    "all that you dream you are that you are only human and that your heart "
    "burns for just a chance at a life with him. Clasping your fingers in his "
    "and holding so tight you feel like you might never let go to ground "
    "yourself because this might be the last time you ever get to hold him. "
    "Crying yourself to sleep at night because you know in a different life "
    "you could have been sleeping in his arms.\n\nCouldn’t be me lmao."
)

# The "Stop Posting About Among Us" copypasta (biggayrapper, 2021) — verbatim.
_AMONG_US_COPYPASTA = (
    "Stop posting about Among Us! I'm tired of seeing it! My friends on TikTok "
    "send me memes, on Discord it's fucking memes! I was in a server, right? "
    "And all of the channels are just Among Us stuff. I showed my Champion "
    "underwear to my girlfriend and the logo, I flipped it and I said, \"Hey, "
    "babe, when the underwear is sus!\" Haha, ding ding ding ding ding ding "
    "ding, ding-ding-ding! I fucking looked at a trashcan and I said, \"That's "
    "a bit sussy!\" I looked at my penis, I think of an astronaut's helmet and "
    "I go, \"Penis? More like pen-sus!\" Aaaaaaargh!"
)

# 'kys'-type triggers get a deflection, not a straight answer — reversal /
# non-sequitur register, one picked at random. Never instructs self-harm.
_KYS_LINES = (
    "no ❤️",
    "no u",
    "counteroffer: no",
    "skill issue",
    "have you tried logging off instead",
)

# The "Google en passant / Holy hell" Anarchy Chess reply chain.
_HOLY_HELL_CHAIN = (
    "Holy hell!\n"
    "New response just dropped\n"
    "Actual zombie\n"
    "Call the exorcist!\n"
    "Bishop goes on vacation, never comes back\n"
    "Google en passant"
)

# The "don't care + ratio" pile-on (racial/slur variants intentionally dropped).
_L_RATIO_COPYPASTA = (
    "don't care + didn't ask + cry about it + stay mad + get real + L + mald "
    "seethe cope harder + hoes mad + basic + skill issue + ratio + you fell "
    "off + the audacity + triggered + any askers + redpilled + get a life + "
    "ok and? + cringe + touch grass + not based + not funny didn't laugh + "
    "you're* + grammar issue + go outside + get good + reported + ad hominem + GG!"
)

# 'copium' → one of a few interchangeable bits (exercises the tuple branch).
_COPIUM_LINES = (
    "*inhales from the copium tank* 🛢️😤",
    "maximum copium levels detected",
    "you're gonna need a bigger copium tank for this one",
)

# The Zero Wing intro (1992), "All your base are belong to us" — verbatim.
_ALL_YOUR_BASE = (
    "Narrator: In A.D. 2101, war was beginning.\n"
    "Captain: What happen?\n"
    "Mechanic: Somebody set up us the bomb.\n"
    "Operator: We get signal.\n"
    "Captain: What!\n"
    "Operator: Main screen turn on.\n"
    "Captain: It's you!!\n"
    "CATS: How are you gentlemen!!\n"
    "CATS: All your base are belong to us.\n"
    "CATS: You are on the way to destruction.\n"
    "Captain: What you say!!\n"
    "CATS: You have no chance to survive make your time.\n"
    "CATS: Ha Ha Ha Ha....\n"
    "Operator: Captain!!\n"
    "Captain: Take off every 'Zig'!!\n"
    "Captain: You know what you doing.\n"
    "Captain: Move 'Zig'.\n"
    "Captain: For great justice."
)

# The Richard Stallman "I'd just like to interject for a moment" GNU/Linux
# copypasta. Fires on any bare mention of 'linux' — that's the whole joke.
_GNU_LINUX_PASTA = (
    "I'd just like to interject for a moment. What you're referring to as "
    "Linux, is in fact, GNU/Linux, or as I've recently taken to calling it, "
    "GNU plus Linux. Linux is not an operating system unto itself, but rather "
    "another free component of a fully functioning GNU system made useful by "
    "the GNU corelibs, shell utilities and vital system components comprising "
    "a full OS as defined by POSIX.\n\n"
    "Many computer users run a modified version of the GNU system every day, "
    "without realizing it. Through a peculiar turn of events, the version of "
    "GNU which is widely used today is often called \"Linux\", and many of its "
    "users are not aware that it is basically the GNU system, developed by the "
    "GNU Project.\n\n"
    "There really is a Linux, and these people are using it, but it is just a "
    "part of the system they use. Linux is the kernel: the program in the "
    "system that allocates the machine's resources to the other programs that "
    "you run. The kernel is an essential part of an operating system, but "
    "useless by itself; it can only function in the context of a complete "
    "operating system. Linux is normally used in combination with the GNU "
    "operating system: the whole system is basically GNU with Linux added, or "
    "GNU/Linux. All the so-called \"Linux\" distributions are really "
    "distributions of GNU/Linux."
)

# The Unidan "jackdaw is a crow" copypasta (r/AskReddit, 2014) — verbatim.
_JACKDAW_PASTA = (
    "Here's the thing. You said a \"jackdaw is a crow.\"\n\n"
    "Is it in the same family? Yes. No one's arguing that.\n\n"
    "As someone who is a scientist who studies crows, I am telling you, "
    "specifically, in science, no one calls jackdaws crows. If you want to be "
    "\"specific\" like you said, then you shouldn't either. They're not the "
    "same thing.\n\n"
    "If you're saying \"crow family\" you're referring to the taxonomic "
    "grouping of Corvidae, which includes things from nutcrackers to blue jays "
    "to ravens.\n\n"
    "So your reasoning for calling a jackdaw a crow is because random people "
    "\"call the black ones crows?\" Let's get grackles and blackbirds in "
    "there, then, too.\n\n"
    "Also, calling someone a human or an ape? It's not one or the other, "
    "that's not how taxonomy works. They're both. A jackdaw is a jackdaw and a "
    "member of the crow family. But that's not what you said. You said a "
    "jackdaw is a crow, which is not true unless you're okay with calling all "
    "members of the crow family crows, which means you'd call blue jays, "
    "ravens, and other birds crows, too. Which you said you don't.\n\n"
    "It's okay to just admit you're wrong, you know?"
)

# 'rizz' → interchangeable brainrot bits (none of them echo the word alone).
_RIZZ_LINES = (
    "unspoken rizz detected 🕴️",
    "certified rizzler moment",
    "rizz level: unemployed",
)

# 'let him cook' → for/against, at random.
_COOK_LINES = (
    "*hands him the apron* 🧑‍🍳",
    "he is NOT cooking. someone check the kitchen. 🚒",
)

# ─────────────────────────────────────────────────────────────────────────────
# Media responses — the actual meme, verified content at pin time.
# ─────────────────────────────────────────────────────────────────────────────
_M_PIKACHU = MediaResponse(
    "photo", "https://i.imgflip.com/2kbn1e.jpg",
    "⚡", "*surprised Pikachu face* ⚡")
_M_STONKS = MediaResponse(
    "photo",
    "https://i.kym-cdn.com/entries/icons/facebook/000/029/959/"
    "Screen_Shot_2019-06-05_at_1.26.32_PM.jpg",
    "📈", "📈 line goes up.")
_M_PIGEON = MediaResponse(
    "photo", "https://i.imgflip.com/1o00in.jpg",
    "🦋", "🦋 is this a bug report?")
_M_SAME_PICTURE = MediaResponse(
    "photo", "https://i.imgflip.com/2za3u1.jpg",
    "corporate needs you to find the differences between this picture "
    "and this picture",
    "corporate needs you to find the differences between this picture "
    "and this picture. 📷 (it's the same picture.)")
_M_HONEST_WORK = MediaResponse(
    "photo", "https://i.kym-cdn.com/entries/icons/mobile/000/028/021/work.jpg",
    "🌾", "🌾 Dave Brandt would be proud.")
_M_SCIENTIST = MediaResponse(
    "photo", "https://i.imgflip.com/27qxmb.jpg",
    "🧪", "🧪 *Green Goblin cackling*")
_M_DOUBT = MediaResponse(
    "photo",
    "https://i.kym-cdn.com/entries/icons/facebook/000/023/021/"
    "e02e5ffb5f980cd8262cf7f0ae00a4a9_press-x-to-doubt-memes-memesuper-"
    "la-noire-doubt-meme_419-238.jpg",
    "🤨", "[X] Doubt 🤨")
_M_WEDNESDAY = MediaResponse(
    "photo",
    "https://i.kym-cdn.com/entries/icons/facebook/000/020/016/"
    "wednesdaymydudeswide.jpg",
    "my dudes", "AAAAAAAAAAAAAA 🐸")
_M_YOU_DIED = MediaResponse(
    "photo",
    "https://i.kym-cdn.com/entries/icons/facebook/000/029/198/"
    "Dark_Souls_You_Died_Screen_-_Completely_Black_Screen_0-2_screenshot.jpg",
    "bonfire ahead. try jumping.", "bonfire ahead. try jumping. 💀")
_M_GIGACHAD = MediaResponse(
    "photo",
    "https://i.kym-cdn.com/photos/images/facebook/001/896/218/7d4.png",
    "average iPedro enjoyer", "🗿")
_M_MORDOR = MediaResponse(
    "photo", "https://i.imgflip.com/1bij.jpg",
    "🌋", "...walk into Mordor. 🌋")
_M_RICKROLL = MediaResponse(
    "gif", "https://media.giphy.com/media/Vuw9m5wXviFIQ/giphy.gif",
    "never gonna let you down 🕺",
    "never gonna let you down. never gonna run around and desert you. 🕺")
_M_ROAD_WORK = MediaResponse(
    "gif", "https://media1.tenor.com/m/la1OiXDLU4AAAAAd/road-work-ahead-vine.gif",
    "uh yeah, I sure hope it does",
    "uh yeah, I sure hope it does. 🚧")
_M_HASTA = MediaResponse(
    "gif", "https://media1.tenor.com/m/b2NZhgvJEUIAAAAd/terminator-okay.gif",
    "👍", "*thumbs up, sinking into molten steel* 👍")
_M_ZA_WARUDO = MediaResponse(
    "gif", "https://media1.tenor.com/m/vqZK76FJbMYAAAAd/dio-time-stop.gif",
    "TOKI YO TOMARE ⏱️", "*time stops for exactly nine seconds* ⏱️")
_M_WHY_RUNNING = MediaResponse(
    "gif", "https://media.tenor.com/1lUzcGrjPiwAAAAM/twgcf-jjkneko.gif",
    "🏃", "*gun jams* 🏃")

# ─────────────────────────────────────────────────────────────────────────────
# The trigger table. First match wins.
# ─────────────────────────────────────────────────────────────────────────────
_AUTOMOD_TRIGGERS: tuple[
    tuple["re.Pattern[str]", "str | tuple[str, ...] | MediaResponse"], ...
] = (
    # --- kys stays first: it must win over any joke trigger in the message ---
    (re.compile(r"\bkys\b|(kill|neck)\s*(your|my|ur|yr)\s*self", re.IGNORECASE),
     _KYS_LINES),

    # --- long-form copypastas ---
    (_GAY_RE, _GAY_COPYPASTA),
    (re.compile(r"\bamong\s*us\b|\bamogus\b|\bsussy\b", re.IGNORECASE),
     _AMONG_US_COPYPASTA),
    (re.compile(r"\bholy\s+hell\b|\ben\s+passant\b", re.IGNORECASE),
     _HOLY_HELL_CHAIN),
    # 'ratio' alone is too common (aspect ratio, math); only the taunt forms fire.
    (re.compile(r"\bl\s*\+\s*ratio\b|\bratio(?:ed|'?d)\b", re.IGNORECASE),
     _L_RATIO_COPYPASTA),
    (re.compile(r"\ball your base\b", re.IGNORECASE), _ALL_YOUR_BASE),
    (re.compile(r"\blinux\b", re.IGNORECASE), _GNU_LINUX_PASTA),
    (re.compile(r"\bjackdaw\b", re.IGNORECASE), _JACKDAW_PASTA),

    # --- the actual meme, as media ---
    (re.compile(r"\bsurprised pikachu\b", re.IGNORECASE), _M_PIKACHU),
    (re.compile(r"\bstonks\b", re.IGNORECASE), _M_STONKS),
    (re.compile(r"\bis this a pigeon\b", re.IGNORECASE), _M_PIGEON),
    (re.compile(r"\bsame picture\b", re.IGNORECASE), _M_SAME_PICTURE),
    (re.compile(r"\bhonest work\b", re.IGNORECASE), _M_HONEST_WORK),
    (re.compile(r"\bscientist myself\b", re.IGNORECASE), _M_SCIENTIST),
    (re.compile(r"\bx to doubt\b", re.IGNORECASE), _M_DOUBT),
    (re.compile(r"\bit'?s wednesday\b", re.IGNORECASE), _M_WEDNESDAY),
    (re.compile(r"\byou died\b", re.IGNORECASE), _M_YOU_DIED),
    (re.compile(r"\bgigachad\b", re.IGNORECASE), _M_GIGACHAD),
    (re.compile(r"\bone does not simply\b", re.IGNORECASE), _M_MORDOR),
    (re.compile(r"\bnever gonna give you up\b", re.IGNORECASE), _M_RICKROLL),
    (re.compile(r"\broad work ahead\b", re.IGNORECASE), _M_ROAD_WORK),
    (re.compile(r"\bhasta la vista\b", re.IGNORECASE), _M_HASTA),
    (re.compile(r"\bza warudo\b", re.IGNORECASE), _M_ZA_WARUDO),
    (re.compile(r"\bwhy are you running\b", re.IGNORECASE), _M_WHY_RUNNING),

    # --- shitpost / brainrot one-liners (retorts & continuations only) ---
    (re.compile(r"\bbased\b", re.IGNORECASE), "Based? Based on what?"),
    (re.compile(r"\bsneed\b", re.IGNORECASE), "Formerly Chuck's."),
    (re.compile(r"\btrans rights\b", re.IGNORECASE),
     "🏳️‍⚧️ trans rights are human rights."),
    (re.compile(r"\bnl\b", re.IGNORECASE), "Never lucky."),
    (re.compile(r"\bcopium\b", re.IGNORECASE), _COPIUM_LINES),
    (re.compile(r"\bmorb(?:ius|in|ing)\b", re.IGNORECASE),
     "*starts morbing* 🦇"),
    (re.compile(r"\bskibidi\b", re.IGNORECASE), "bop bop bop bop yes yes 🚽"),
    (re.compile(r"\bohio\b", re.IGNORECASE),
     "you can't leave. it's Ohio. 💀"),
    (re.compile(r"\bsigma\b", re.IGNORECASE), "sigma balls. 🥷"),
    (re.compile(r"\brizz\b", re.IGNORECASE), _RIZZ_LINES),
    (re.compile(r"\bwe live in a society\b", re.IGNORECASE),
     "🃏 we live in one. gamers, rise up."),
    (re.compile(r"\bmitochondria\b", re.IGNORECASE),
     "the powerhouse of the cell 🔬"),
    (re.compile(r"\breduced to atoms\b", re.IGNORECASE), "*snaps fingers* 🫰"),
    (re.compile(r"\bnarwhal bacons\b", re.IGNORECASE), "🦄 ...at midnight."),
    (re.compile(r"\bgyat+\b", re.IGNORECASE), "level 10 gyatt detected 🚨"),
    (re.compile(r"\bfanum tax\b", re.IGNORECASE), "not the fanum tax 💀"),
    (re.compile(r"\blet (?:him|her|them) cook\b", re.IGNORECASE), _COOK_LINES),
    (re.compile(r"\breddit moment\b", re.IGNORECASE), "🤓 erm, ackshually"),
    (re.compile(r"\bdeez nuts\b", re.IGNORECASE), "Ha! Got 'em. 🥜"),
    (re.compile(r"\bok boomer\b", re.IGNORECASE), "ok zoomer 👵"),
    (re.compile(r"\btask failed successfully\b", re.IGNORECASE),
     "🪟 Error: The operation completed successfully."),
    (re.compile(r"\btook that personally\b", re.IGNORECASE),
     "*wins six championships about it* 🐐"),
    (re.compile(r"\bmodern problems\b", re.IGNORECASE),
     "...require modern solutions. 🧠"),
    (re.compile(r"\bfollow the damn train\b", re.IGNORECASE),
     "ah shit, here we go again. 🚂"),

    # --- Star Wars / Star Trek ---
    (re.compile(r"\bhello there\b", re.IGNORECASE),
     "General Kenobi! You are a bold one. ⚔️"),
    (re.compile(r"\bhigh ground\b", re.IGNORECASE),
     "It's over, Anakin. I have the high ground!"),
    (re.compile(r"\bi love democracy\b", re.IGNORECASE),
     "somehow, Palpatine returned. 👑"),
    (re.compile(r"\byou were the chosen one\b", re.IGNORECASE),
     "You were supposed to destroy the Sith, not join them!"),
    (re.compile(r"\bthis is where the fun begins\b", re.IGNORECASE),
     "*spins* ...that's a good trick. 🌀"),
    (re.compile(r"\bi am your father\b", re.IGNORECASE), "NOOOOOOO!"),
    (re.compile(r"\bthere is no try\b", re.IGNORECASE), "Do. Or do not. 🐸"),
    (re.compile(r"\black of faith\b", re.IGNORECASE), "*force-chokes* 🖤"),
    (re.compile(r"\bthese aren'?t the droids\b", re.IGNORECASE),
     "move along. move along. 👋"),
    (re.compile(r"\bthis is the way\b", re.IGNORECASE), "I have spoken. 🪖"),
    (re.compile(r"\blive long and prosper\b", re.IGNORECASE),
     "🖖 peace and long life."),
    (re.compile(r"\bresistance is futile\b", re.IGNORECASE),
     "You will be assimilated. 🤖"),

    # --- LOTR ---
    (re.compile(r"\byou shall not pass\b", re.IGNORECASE), "None shall pass. 🐴"),
    (re.compile(r"\band my axe\b", re.IGNORECASE),
     "You have my sword. And my bow. 🏹"),
    (re.compile(r"\bfly,?\s+you fools\b", re.IGNORECASE),
     "*eagle screech in the distance* 🦅"),

    # --- The Office ---
    (re.compile(r"\bhow the turntables\b", re.IGNORECASE),
     "*looks directly into the camera* 📷"),
    (re.compile(r"\bthat'?s what she said\b", re.IGNORECASE),
     "— Michael Scott, probably 📎"),
    (re.compile(r"\bbears\.?\s*beets\b", re.IGNORECASE),
     "Battlestar Galactica. 🐻"),
    (re.compile(r"\bidentity theft\b", re.IGNORECASE),
     "Identity theft is not a joke, Jim! Millions of families suffer every "
     "year! 📠"),

    # --- movies ---
    (re.compile(r"\bi want the truth\b", re.IGNORECASE),
     "You can't handle the truth! ⚖️"),
    (re.compile(r"\bbox of chocolates\b", re.IGNORECASE),
     "You never know what you're gonna get. 🍫"),
    (re.compile(r"\byou talkin['g]? to me\b", re.IGNORECASE),
     "Well, I'm the only one here. 🚕"),
    (re.compile(r"\bbigger boat\b", re.IGNORECASE),
     "🦈 duunnn dunnn... duuuunnnn duun."),
    (re.compile(r"\boffer (?:he|you) can'?t refuse\b", re.IGNORECASE),
     "*a horse head appears in your bed* 🐴"),
    (re.compile(r"\bhouston,? we have a problem\b", re.IGNORECASE),
     "📡 Roger. Stand by, Apollo."),
    (re.compile(r"\bto infinity\b", re.IGNORECASE), "AND BEYOND! 🚀"),
    (re.compile(r"\bjust keep swimming\b", re.IGNORECASE),
     "what do we do? we swim, swim. 🐠"),
    (re.compile(r"\bhakuna matata\b", re.IGNORECASE),
     "what a wonderful phrase! 🦁"),
    (re.compile(r"\bwhy so serious\b", re.IGNORECASE),
     "let's put a smile on that face. 🃏"),
    (re.compile(r"\bhandle the truth\b", re.IGNORECASE),
     "*Colonel Jessep intensifies* ⚖️"),

    # --- Mean Girls & Vine ---
    (re.compile(r"\bmake fetch happen\b", re.IGNORECASE),
     "Stop trying to make fetch happen! It's not going to happen! 💅"),
    (re.compile(r"\bget in loser\b", re.IGNORECASE),
     "We're going shopping. 💅"),
    (re.compile(r"\bon wednesdays we wear pink\b", re.IGNORECASE),
     "you can't sit with us! 💗"),
    (re.compile(r"\bthe limit does not exist\b", re.IGNORECASE),
     "...and just like that, the Mathletes win state. 📈"),
    (re.compile(r"\blook at all those chickens\b", re.IGNORECASE),
     "🐔 (they were, in fact, geese)"),
    (re.compile(r"\bthey were roommates\b", re.IGNORECASE),
     "oh my god, they were roommates. 🏠"),

    # --- video games ---
    (re.compile(r"\bwar never changes\b", re.IGNORECASE),
     "☢️ *vault door creaks open*"),
    (re.compile(r"\bwould you kindly\b", re.IGNORECASE),
     "A man chooses; a slave obeys. 🌊"),
    (re.compile(r"\bfinally awake\b", re.IGNORECASE),
     "you were trying to cross the border, right? 🐴"),
    (re.compile(r"\barrow (?:in|to) the knee\b", re.IGNORECASE),
     "I used to be an adventurer like you. Then I took an arrow to the knee. 🏹"),
    (re.compile(r"\bpraise the sun\b", re.IGNORECASE), "\\[T]/ ☀️"),
    (re.compile(r"\bfinish him\b", re.IGNORECASE), "FATALITY. 💀"),
    (re.compile(r"\bfatality\b", re.IGNORECASE), "FLAWLESS VICTORY. 🥋"),
    (re.compile(r"\bbarrel roll\b", re.IGNORECASE), "(press Z or R twice) 🚀"),
    (re.compile(r"\bdangerous to go alone\b", re.IGNORECASE),
     "take this. 🗡️"),
    (re.compile(r"\bobjection\b", re.IGNORECASE), "OVERRULED. ⚖️"),
    (re.compile(r"\badditional pylons\b", re.IGNORECASE),
     "not enough minerals. 🔮"),
    (re.compile(r"\bleeroy\b", re.IGNORECASE),
     "at least I have chicken. 🍗"),
    (re.compile(r"\bfor the horde\b", re.IGNORECASE), "FOR THE ALLIANCE! ⚔️"),
    (re.compile(r"\banother castle\b", re.IGNORECASE),
     "Thank you Mario! But our princess is in another castle! 🍄"),
    (re.compile(r"\bit'?s[- ]?a me\b", re.IGNORECASE), "Mama mia! 🍄"),
    (re.compile(r"\bsuper effective\b", re.IGNORECASE),
     "*it's not very effective...* ⚡"),
    (re.compile(r"\bhadouken\b", re.IGNORECASE), "SHORYUKEN! 🥊"),
    (re.compile(r"\bget over here\b", re.IGNORECASE),
     "*harpoon through the chest* 🔗"),
    (re.compile(r"\bgit gud\b", re.IGNORECASE),
     "git: 'gud' is not a git command. See 'git --help'."),
    (re.compile(r"\bdysentery\b", re.IGNORECASE),
     "You have died of dysentery. 🐂"),
    (re.compile(r"\bpay respects\b", re.IGNORECASE), "F"),

    # --- anime ---
    (re.compile(r"\bover 9000\b", re.IGNORECASE),
     "WHAT?! 9000?! There's no way that can be right! 🔥"),
    (re.compile(r"\bjojo reference\b", re.IGNORECASE), "ゴゴゴゴ (menacing) 🕶️"),
    (re.compile(r"\bnothing personnel\b", re.IGNORECASE),
     "*teleports behind you* psh... heh... 🌀"),
    (re.compile(r"\bomae wa mou\b", re.IGNORECASE), "NANI?! 💥"),
    (re.compile(r"\bkamehameha\b", re.IGNORECASE), "*your scouter explodes* 💥"),
    (re.compile(r"\bora ora\b", re.IGNORECASE), "MUDA MUDA MUDA! 🧛"),
    (re.compile(r"\byare yare\b", re.IGNORECASE), "good grief. 🕶️"),
    (re.compile(r"\bkeikaku\b", re.IGNORECASE),
     "just as planned. (TL note: keikaku means plan) 📝"),
    (re.compile(r"\bdattebayo\b", re.IGNORECASE), "believe it! 🍥"),

    # --- other classics ---
    (re.compile(r"\bthe cake is a lie\b", re.IGNORECASE),
     "this was a triumph. I'm making a note here: HUGE SUCCESS. 🎂"),
    (re.compile(r"\bwake me up inside\b", re.IGNORECASE), "(can't wake up) 🎸"),
    (re.compile(r"\bsomebody once told me\b", re.IGNORECASE),
     "the world is gonna roll me 🌍"),
    (re.compile(r"\bogres are like onions\b", re.IGNORECASE),
     "ogres have LAYERS. 🧅"),
    (re.compile(r"\bthis is a wendy'?s\b", re.IGNORECASE),
     "may I take your order? 🍔"),
    (re.compile(r"\bi also choose this guy\b", re.IGNORECASE),
     "...and I also choose this guy's dead wife. 💀"),

    # --- number jokes (kept last: most incidental) ---
    (re.compile(r"(?<!\d)69(?!\d)"), "nice"),
    (re.compile(r"(?<!\d)420(?!\d)"), "blaze it 🔥"),
)


def _automod_response(
    text: str | None, rng: random.Random | None = None,
) -> "str | MediaResponse | None":
    """First matching AutoMod-style canned response for `text`, or None."""
    if not text:
        return None
    r = rng or random
    for pattern, response in _AUTOMOD_TRIGGERS:
        if pattern.search(text):
            if isinstance(response, MediaResponse):
                return response
            return response if isinstance(response, str) else r.choice(response)
    return None
