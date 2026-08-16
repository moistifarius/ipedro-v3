"""AutoModerator-style canned responses — real r/shitposting bits & copypastas.

Keyword -> canned reply, in the spirit of the r/shitposting AutoModerator.
First match wins; this is consulted before the normal AI reply. Each response
is either a single string or a tuple of strings (one picked at random).

`_AUTOMOD_TRIGGERS` IS THE WHOLE EXTENSION POINT — add a (regex, response) row.

Design notes:
- Every pattern is anchored with word boundaries / lookarounds so common words
  don't trip a wall of text (e.g. 'ratio' vs 'aspect ratio', standalone 'nl').
- Patterns use only simple alternations and bounded quantifiers — no nested
  quantifiers — so there is no catastrophic-backtracking (ReDoS) risk.
- The one-time serious case ('kys') is deliberately NOT played straight; it
  returns a deflection that never instructs self-harm. See `_KYS_LINES`.
- Responses are static constants: no user input is ever interpolated, so there
  is no injection surface.
"""

from __future__ import annotations

import random
import re

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

# 'rizz' → one of a few interchangeable brainrot bits.
_RIZZ_LINES = (
    "W rizz 😤",
    "unspoken rizz detected",
    "certified rizzler moment",
)

# ─────────────────────────────────────────────────────────────────────────────
# The trigger table. First match wins.
# ─────────────────────────────────────────────────────────────────────────────
_AUTOMOD_TRIGGERS: tuple[tuple["re.Pattern[str]", "str | tuple[str, ...]"], ...] = (
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

    # --- short shitpost / brainrot one-liners ---
    (re.compile(r"\bbased\b", re.IGNORECASE), "Based? Based on what?"),
    (re.compile(r"\bsneed\b", re.IGNORECASE), "Formerly Chuck's."),
    (re.compile(r"\btrans rights\b", re.IGNORECASE),
     "🏳️‍⚧️ trans rights are human rights."),
    (re.compile(r"\bnl\b", re.IGNORECASE), "Never lucky."),
    (re.compile(r"\bcopium\b", re.IGNORECASE), _COPIUM_LINES),
    (re.compile(r"\bmorb(?:ius|in|ing)\b", re.IGNORECASE), "It's Morbin' Time."),
    (re.compile(r"\bskibidi\b", re.IGNORECASE), "skibidi bop bop yes yes 🚽"),
    (re.compile(r"\bohio\b", re.IGNORECASE), "only in Ohio 💀"),
    (re.compile(r"\bsigma\b", re.IGNORECASE), "what the sigma?"),
    (re.compile(r"\brizz\b", re.IGNORECASE), _RIZZ_LINES),
    (re.compile(r"\bwe live in a society\b", re.IGNORECASE),
     "🃏 we live in one. gamers, rise up."),
    (re.compile(r"\bmitochondria\b", re.IGNORECASE),
     "the powerhouse of the cell 🔬"),
    (re.compile(r"\breduced to atoms\b", re.IGNORECASE),
     "Gone. Reduced to atoms. ⚛️"),
    (re.compile(r"\bnarwhal bacons\b", re.IGNORECASE), "🦄 ...at midnight."),

    # --- Star Wars ---
    (re.compile(r"\bhello there\b", re.IGNORECASE),
     "General Kenobi! You are a bold one. ⚔️"),
    (re.compile(r"\bhigh ground\b", re.IGNORECASE),
     "It's over, Anakin. I have the high ground!"),
    (re.compile(r"\bi love democracy\b", re.IGNORECASE),
     "I love democracy. I love the Republic. 🌌"),
    (re.compile(r"\byou were the chosen one\b", re.IGNORECASE),
     "You were supposed to destroy the Sith, not join them!"),
    (re.compile(r"\bthis is where the fun begins\b", re.IGNORECASE),
     "This is where the fun begins. 🚀"),

    # --- Lord of the Rings ---
    (re.compile(r"\bone does not simply\b", re.IGNORECASE),
     "...walk into Mordor. 🌋"),
    (re.compile(r"\byou shall not pass\b", re.IGNORECASE), "🧙 YOU SHALL NOT PASS!"),
    (re.compile(r"\band my axe\b", re.IGNORECASE), "And my bow! ...And my axe! 🪓"),
    (re.compile(r"\bfly,?\s+you fools\b", re.IGNORECASE), "Fly, you fools! 🧙"),

    # --- The Office ---
    (re.compile(r"\bhow the turntables\b", re.IGNORECASE),
     "Well, well, well. How the turntables..."),
    (re.compile(r"\bthat'?s what she said\b", re.IGNORECASE),
     "That's what she said. 😏"),
    (re.compile(r"\bbears\.?\s*beets\b", re.IGNORECASE),
     "Bears. Beets. Battlestar Galactica."),
    (re.compile(r"\bidentity theft\b", re.IGNORECASE),
     "Identity theft is not a joke, Jim! Millions of families suffer every "
     "year! 📠"),

    # --- video games ---
    (re.compile(r"\bwar never changes\b", re.IGNORECASE),
     "War. War never changes. ☢️"),
    (re.compile(r"\bwould you kindly\b", re.IGNORECASE),
     "A man chooses; a slave obeys. 🌊"),
    (re.compile(r"\bfinally awake\b", re.IGNORECASE),
     "Hey, you. You're finally awake. 🐴"),
    (re.compile(r"\barrow (?:in|to) the knee\b", re.IGNORECASE),
     "I used to be an adventurer like you. Then I took an arrow to the knee. 🏹"),
    (re.compile(r"\bpraise the sun\b", re.IGNORECASE), r"\[T]/ Praise the Sun! ☀️"),
    (re.compile(r"\byou died\b", re.IGNORECASE), "YOU DIED 💀"),
    (re.compile(r"\bfinish him\b", re.IGNORECASE), "FINISH HIM! 🩸"),
    (re.compile(r"\bbarrel roll\b", re.IGNORECASE), "Do a barrel roll! 🚀"),
    (re.compile(r"\bdangerous to go alone\b", re.IGNORECASE),
     "It's dangerous to go alone! Take this. 🗡️"),
    (re.compile(r"\bobjection\b", re.IGNORECASE), "OBJECTION! ⚖️"),

    # --- anime ---
    (re.compile(r"\bover 9000\b", re.IGNORECASE),
     "WHAT?! 9000?! There's no way that can be right! 🔥"),
    (re.compile(r"\bjojo reference\b", re.IGNORECASE),
     "Is this a JoJo reference?! 🕶️"),
    (re.compile(r"\bnothing personnel\b", re.IGNORECASE),
     "*teleports behind you* Nothing personnel, kid. 🌀"),
    (re.compile(r"\bomae wa mou\b", re.IGNORECASE),
     "お前はもう死んでいる。\n\nNANI?! 💥"),
    (re.compile(r"\bplus ultra\b", re.IGNORECASE), "PLUS ULTRA! 💪"),

    # --- other classics ---
    (re.compile(r"\bthe cake is a lie\b", re.IGNORECASE), "The cake is a lie. 🎂"),
    (re.compile(r"\bwhy so serious\b", re.IGNORECASE), "Why so serious? 🃏"),
    (re.compile(r"\bwake me up inside\b", re.IGNORECASE), "(I can't wake up) 🎸"),
    (re.compile(r"\bnever gonna give you up\b", re.IGNORECASE),
     "Never gonna let you down. Never gonna run around and desert you. 🕺"),
    (re.compile(r"\bsomebody once told me\b", re.IGNORECASE),
     "the world is gonna roll me 🌍"),
    (re.compile(r"\bogres are like onions\b", re.IGNORECASE),
     "Ogres have layers. Onions have layers. 🧅"),
    (re.compile(r"\bthis is a wendy'?s\b", re.IGNORECASE), "Sir, this is a Wendy's. 🍔"),
    (re.compile(r"\bit'?s wednesday\b", re.IGNORECASE),
     "It is Wednesday, my dudes. 🐸 AAAAAAAAAAAAAA"),
    (re.compile(r"\bi also choose this guy\b", re.IGNORECASE),
     "...and I also choose this guy's dead wife."),

    # --- more Star Wars / Star Trek ---
    (re.compile(r"\bi am your father\b", re.IGNORECASE), "No. I am your father. ⚡"),
    (re.compile(r"\bthere is no try\b", re.IGNORECASE),
     "Do. Or do not. There is no try. 🐸"),
    (re.compile(r"\black of faith\b", re.IGNORECASE),
     "I find your lack of faith disturbing. 🖤"),
    (re.compile(r"\bthese aren'?t the droids\b", re.IGNORECASE),
     "These aren't the droids you're looking for. 👋"),
    (re.compile(r"\bthis is the way\b", re.IGNORECASE), "This is the Way. 🪖"),
    (re.compile(r"\bhasta la vista\b", re.IGNORECASE), "Hasta la vista, baby. 🤖"),
    (re.compile(r"\blive long and prosper\b", re.IGNORECASE),
     "🖖 Live long and prosper."),
    (re.compile(r"\bresistance is futile\b", re.IGNORECASE),
     "Resistance is futile. You will be assimilated. 🤖"),

    # --- more movies ---
    (re.compile(r"\bhandle the truth\b", re.IGNORECASE),
     "You can't handle the truth! ⚖️"),
    (re.compile(r"\bbox of chocolates\b", re.IGNORECASE),
     "Life is like a box of chocolates. You never know what you're gonna get. 🍫"),
    (re.compile(r"\byou talkin['g]? to me\b", re.IGNORECASE), "You talkin' to me? 🚕"),
    (re.compile(r"\bbigger boat\b", re.IGNORECASE),
     "You're gonna need a bigger boat. 🦈"),
    (re.compile(r"\boffer (?:he|you) can'?t refuse\b", re.IGNORECASE),
     "I'm gonna make him an offer he can't refuse. 🎻"),
    (re.compile(r"\bhouston,? we have a problem\b", re.IGNORECASE),
     "Houston, we have a problem. 🚀"),
    (re.compile(r"\bshow me the money\b", re.IGNORECASE), "SHOW ME THE MONEY! 💰"),
    (re.compile(r"\bto infinity\b", re.IGNORECASE), "To infinity... and beyond! 🚀"),
    (re.compile(r"\bjust keep swimming\b", re.IGNORECASE), "Just keep swimming. 🐠"),
    (re.compile(r"\bhakuna matata\b", re.IGNORECASE),
     "Hakuna Matata! What a wonderful phrase 🦁"),

    # --- Mean Girls & Vine ---
    (re.compile(r"\bmake fetch happen\b", re.IGNORECASE),
     "Stop trying to make fetch happen! It's not going to happen! 💅"),
    (re.compile(r"\bget in loser\b", re.IGNORECASE),
     "Get in, loser. We're going shopping. 💅"),
    (re.compile(r"\bon wednesdays we wear pink\b", re.IGNORECASE),
     "On Wednesdays we wear pink. 💗"),
    (re.compile(r"\bthe limit does not exist\b", re.IGNORECASE),
     "The limit does not exist! 📈"),
    (re.compile(r"\broad work ahead\b", re.IGNORECASE),
     "Road work ahead? Uh, yeah, I sure hope it does. 🚧"),
    (re.compile(r"\bwhat are those\b", re.IGNORECASE), "WHAT ARE THOOOSE?! 👟"),
    (re.compile(r"\blook at all those chickens\b", re.IGNORECASE),
     "WOAH. Look at all those chickens! 🐔"),
    (re.compile(r"\bthey were roommates\b", re.IGNORECASE),
     "and they were ROOMMATES 🏠 (oh my god they were roommates)"),
    (re.compile(r"\bmy name is jeff\b", re.IGNORECASE), "...my name is Jeff. 🕶️"),

    # --- more video games ---
    (re.compile(r"\badditional pylons\b", re.IGNORECASE),
     "You must construct additional pylons. 🔮"),
    (re.compile(r"\bleeroy\b", re.IGNORECASE), "LEEEEEROY JENKINS! 🍗"),
    (re.compile(r"\bfor the horde\b", re.IGNORECASE), "FOR THE HORDE! ⚔️"),
    (re.compile(r"\banother castle\b", re.IGNORECASE),
     "Thank you Mario! But our princess is in another castle! 🍄"),
    (re.compile(r"\bit'?s[- ]?a me\b", re.IGNORECASE), "It's-a me, Mario! 🍄"),
    (re.compile(r"\bgotta go fast\b", re.IGNORECASE), "Gotta go fast! 💨"),
    (re.compile(r"\bsuper effective\b", re.IGNORECASE), "It's super effective! ⚡"),
    (re.compile(r"\bhadouken\b", re.IGNORECASE), "HADOUKEN! 🔥"),
    (re.compile(r"\bfatality\b", re.IGNORECASE), "FATALITY. 💀"),
    (re.compile(r"\bflawless victory\b", re.IGNORECASE), "FLAWLESS VICTORY 🥋"),
    (re.compile(r"\bget over here\b", re.IGNORECASE), "GET OVER HERE! 🔗"),
    (re.compile(r"\bgit gud\b", re.IGNORECASE), "git gud 🎮"),
    (re.compile(r"\bdysentery\b", re.IGNORECASE), "You have died of dysentery. 🐂"),
    (re.compile(r"\bpay respects\b", re.IGNORECASE), "Press F to pay respects. 🫡"),

    # --- more anime ---
    (re.compile(r"\bkamehameha\b", re.IGNORECASE), "KAAA-MEEE-HAAA-MEEE-HAAA! 💥"),
    (re.compile(r"\bora ora\b", re.IGNORECASE), "ORA ORA ORA ORA! 🌟"),
    (re.compile(r"\byare yare\b", re.IGNORECASE), "Yare yare daze... 🕶️"),
    (re.compile(r"\bza warudo\b", re.IGNORECASE), "ZA WARUDO! ⏱️"),
    (re.compile(r"\bkeikaku\b", re.IGNORECASE),
     "Just as planned. (Keikaku means 'plan'.) 📝"),
    (re.compile(r"\bdattebayo\b", re.IGNORECASE), "Believe it! 🍥"),

    # --- reaction memes & brainrot ---
    (re.compile(r"\bsurprised pikachu\b", re.IGNORECASE),
     "*surprised Pikachu face* ⚡"),
    (re.compile(r"\bsame picture\b", re.IGNORECASE), "They're the same picture. 📷"),
    (re.compile(r"\bis this a pigeon\b", re.IGNORECASE), "Is this a pigeon? 🦋"),
    (re.compile(r"\btook that personally\b", re.IGNORECASE),
     "And I took that personally. 🐐"),
    (re.compile(r"\bhonest work\b", re.IGNORECASE),
     "It ain't much, but it's honest work. 🌾"),
    (re.compile(r"\bmodern problems\b", re.IGNORECASE),
     "Modern problems require modern solutions. 🧠"),
    (re.compile(r"\bfollow the damn train\b", re.IGNORECASE),
     "All we had to do was follow the damn train, CJ! 🚂"),
    (re.compile(r"\bwhy are you running\b", re.IGNORECASE),
     "Why are you running?! Why are you running?! 🏃"),
    (re.compile(r"\bscientist myself\b", re.IGNORECASE),
     "I'm something of a scientist myself. 🧪"),
    (re.compile(r"\bx to doubt\b", re.IGNORECASE), "[X] Doubt 🤨"),
    (re.compile(r"\btask failed successfully\b", re.IGNORECASE),
     "Task failed successfully. ✅"),
    (re.compile(r"\bstonks\b", re.IGNORECASE), "📈 Stonks."),
    (re.compile(r"\bok boomer\b", re.IGNORECASE), "ok boomer 👵"),
    (re.compile(r"\bbazinga\b", re.IGNORECASE), "Bazinga. 🤓"),
    (re.compile(r"\bgigachad\b", re.IGNORECASE), "🗿"),
    (re.compile(r"\bgyat+\b", re.IGNORECASE), "GYATT 🍑"),
    (re.compile(r"\bfanum tax\b", re.IGNORECASE), "not the fanum tax 💀"),
    (re.compile(r"\blet him cook\b", re.IGNORECASE), "let him cook 🔥"),
    (re.compile(r"\breddit moment\b", re.IGNORECASE), "reddit moment 🤓"),
    (re.compile(r"\bdeez nuts\b", re.IGNORECASE), "Ha! Got 'em. 🥜"),

    # --- number jokes (kept last: most incidental) ---
    (re.compile(r"(?<!\d)69(?!\d)"), "nice"),
    (re.compile(r"(?<!\d)420(?!\d)"), "blaze it 🔥"),
)


def _automod_response(text: str | None, rng: random.Random | None = None) -> str | None:
    """First matching AutoMod-style canned response for `text`, or None."""
    if not text:
        return None
    r = rng or random
    for pattern, response in _AUTOMOD_TRIGGERS:
        if pattern.search(text):
            return response if isinstance(response, str) else r.choice(response)
    return None
