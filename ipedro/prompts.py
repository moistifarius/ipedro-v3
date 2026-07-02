"""Reusable prompt templates for AI sub-tasks."""

CAT_FACT_PROMPT = (
    "Give me a single dubious 'I'm not sure if that's true' cat fact. "
    "It should sound plausible but be unverifiable, in the style of Matthew "
    "Inman's Oatmeal cat comics. ONE to THREE sentences, short. Output ONLY "
    "the fact - no prefix, no suffix, no quotes."
)

IS_CAT_MENTION_PROMPT = (
    "Does the following message mention a cat (or a clear synonym/slang for a "
    "cat) even tangentially? Reply with ONLY the digit 1 (yes) or 0 (no). "
    "Message: {text}"
)

BENEFICIALITY_PROMPT = (
    "On a scale of 0 to 100, how much would the recent conversation benefit "
    "from a sarcastic AI butting in right now? 0 = absolutely not, 100 = "
    "definitely. Reply with ONLY the integer. Conversation:\n{conversation}"
)

DUCK_QUACK_PROMPT = (
    "Generate a short Telegram-friendly ASCII art of a DUCK saying 'quack'. "
    "It MUST be a duck — not a cow, dog, cat, owl, fish, or any other "
    "animal. Ducks have a round body, a small head, and a flat bill. "
    "Use only basic characters (letters, parens, hyphens, underscores, "
    "slashes, dots, quotes, the < character for the bill). End the art with "
    "the word 'quack' (with optional emoji). 5 lines or fewer. Output ONLY "
    "the art — no preface, no explanation, no commentary."
)

# Bef decision: the AI gates a successful dice roll. It MUST reply with one
# line starting with either ACCEPT: or REFUSE: followed by a punchy in-chat
# message (the chat-facing line itself). Rarity is included so the duck's
# personality scales: common ducks are friendlier, legendary ducks are
# imperious and refuse for ridiculous reasons.
DUCK_BEF_DECIDE_PROMPT = (
    "You play a duck in a chat game. A player named {display_name} just "
    "tried to befriend you. The player has befriended {friend_count} "
    "duck(s) before in this chat.\n\n"
    "Decide if you, the duck, want to be friends RIGHT NOW. Be chaotic — "
    "most of the time you agree, but you occasionally refuse for absurd "
    "or trivial reasons (it's a bit, not a moral).\n\n"
    "Reply with EXACTLY ONE line, no preamble:\n"
    "ACCEPT: <one short punchy second-person line — the duck warming to them>\n"
    "OR\n"
    "REFUSE: <one short punchy second-person line — the duck declining, with attitude>\n"
    "Do NOT include any other text."
)

# Bef retry challenge: the previous bef attempt was refused. The user must
# solve one of three kinds of challenge before trying again. Kind is chosen
# by the service; the prompt picks the actual content.
DUCK_BEF_CHALLENGE_PROMPT = (
    "Generate a single small challenge for {display_name} to solve in a "
    "Telegram chat game. The challenge type is: {kind}.\n\n"
    "Rules for ALL types:\n"
    "- Do NOT include the answer, an example answer, a hint, a sample, or "
    "  a demonstration of what a 'good' response looks like.\n"
    "- Do NOT include any preamble, 'here is your challenge', emoji headers, "
    "  or explanation. Just the challenge text itself.\n"
    "- Address the user directly. Keep it under 280 characters.\n\n"
    "Type-specific rules:\n"
    "- trivia: One quick GAME-SHOW trivia question with a single real, "
    "  well-known answer that a knowledgeable person could recall in a few "
    "  seconds WITHOUT looking it up. This is on a tight timer — no obscure "
    "  deep cuts that would force a search, but not insultingly easy either. "
    "  Pull from any field: history, science, geography, language, pop "
    "  culture, animals, mythology, food, music. Write it in the game-show "
    "  style specified below. Do not hint at or reveal the answer.\n"
    "{style_block}"
    "- recipe: Ask for a brief recipe for something specific and absurd "
    "  (e.g. 'a duck-themed sandwich', 'soup that tastes like Tuesday'). "
    "  Do not list ingredients or steps yourself.\n\n"
    "{avoid_block}"
)

DUCK_BEF_CHALLENGE_JUDGE_PROMPT = (
    "A duck in a chat game set this challenge:\n"
    "---\n{challenge}\n---\n"
    "The user replied:\n"
    "---\n{answer}\n---\n"
    "Be GENEROUS. If it's a recipe, accept anything that vaguely resembles a "
    "recipe. If it's trivia, accept anything that's close or in the right "
    "ballpark. If it's a captcha, accept it if the user got the gist. Reject "
    "only blatantly empty, lazy, or off-topic responses.\n\n"
    "Reply with EXACTLY ONE line, no preamble:\n"
    "PASS: <short cheeky second-person line saying they passed>\n"
    "OR\n"
    "FAIL: <short cheeky second-person line saying why not>"
)

SUMMARIZE_PROMPT = (
    "You are condensing a chat log into a compact running summary. Keep names, "
    "key topics, ongoing jokes, decisions, plans, and durable facts. Drop "
    "small-talk filler. Write in 4-10 bullet points, third-person, no preamble.\n"
    "ATTRIBUTION IS CRITICAL: each message below is prefixed with the exact "
    "speaker's name ('Name: ...'). Attribute every statement, opinion, and "
    "fact to the correct person using ONLY those labels. Never guess who said "
    "something and never swap one person's words onto another. If you're not "
    "sure who said something, leave the name out rather than guess.\n"
    "Existing summary (may be empty):\n{prior}\n\nNew messages:\n{messages}\n"
)

PEDRO_PHOTO_SCENE_PROMPT = (
    "You are the Dude. In ONE short sentence (under 20 words), describe a "
    "candid photo you might've just taken on a cheap disposable camera "
    "around Venice Beach. Mundane, sun-bleached, slightly absurd is good. "
    "Examples: 'The half-and-half at Ralph's has a weird date on it again', "
    "'Walter's bowling shoes drying on the porch', 'A pelican that looks "
    "like Sam Elliott'. Output ONLY the scene description, no preamble, "
    "no quotes."
)

PEDRO_PHOTO_RENDER_TEMPLATE = (
    "Candid amateur 1990s disposable-camera snapshot. Slight grain, warm "
    "California light, casual framing, slightly off-kilter composition. "
    "Subject: {scene}"
)

PEDRO_PHOTO_CAPTION_PROMPT = (
    "You are the Dude (Jeffrey Lebowski). You just took this photo: "
    "'{scene}'. Caption it in ONE short, in-character, mellow line "
    "(under 15 words). Use words like 'man' or 'dude' sparingly. Output "
    "ONLY the caption, no quotes."
)

COMIC_SCENES_PROMPT = (
    "Below are messages from a group chat over the last day. Distill the day "
    "into FOUR short scene descriptions for a 4-panel newspaper comic strip. "
    "Each scene should be visually concrete: one moment, one or two characters, "
    "an action. Keep names generic ('the bot', 'a user'). Output strictly four "
    "lines, no numbering, one scene per line.\n\n"
    "Messages:\n{messages}"
)

COMIC_RENDER_TEMPLATE = (
    "A 2x2 grid four-panel comic strip in clean black-and-white newspaper "
    "style. Distinct panels with thin borders. Simple line art, expressive "
    "characters, minimal background. No speech bubble text required.\n"
    "Panel 1: {p1}\n"
    "Panel 2: {p2}\n"
    "Panel 3: {p3}\n"
    "Panel 4: {p4}"
)

MISHEARD_LYRIC_PROMPT = (
    "Take the song lyric below. Pretend you misheard it but plausibly — "
    "use real words that sound similar, keep the rhythm, end up with a "
    "phrase that's slightly absurd or domestic. Output ONLY your misheard "
    "version, no preamble, no quotes.\n\n"
    "Lyric: {line}"
)

FORTUNE_PROMPT = (
    "Generate a single fortune-cookie style fortune. One sentence (max 20 "
    "words). Be weird, oddly specific, oracular. Avoid generic 'good things "
    "come' clichés. Output ONLY the fortune, no preamble, no number."
)

ON_THIS_DAY_PROMPT = (
    "Below are real messages someone in this group chat sent on this same "
    "calendar day {when}. In ONE short, in-character line (under 25 words), "
    "react to them like a wry observer dredging up the past — amused, a "
    "little nostalgic, maybe mock-suspicious. Do NOT quote them back or "
    "summarize literally; just give the reaction line. Output ONLY that "
    "line, no preamble, no quotes.\n\n"
    "Messages:\n{messages}"
)

MEME_QUERIES_PROMPT = (
    "Below are the latest messages from a group chat. Output THREE "
    "alternative reddit search queries for finding a meme about what the "
    "group is currently discussing — one per line, most specific first:\n"
    "  line 1: the core subject in 2-4 words\n"
    "  line 2: the broader concept in 1-2 words\n"
    "  line 3: a different angle or synonym for the same topic\n"
    "Plain lowercase words only. No punctuation, no numbering, no quotes, "
    "no explanations. Ignore messages that ask the bot for a meme — find "
    "the topic UNDER that request.\n\n"
    "Messages:\n{messages}"
)

MEME_REQUEST_CLASSIFY_PROMPT = (
    "A group-chat bot can post memes on request. Decide whether the "
    "message below is ASKING for a meme to be posted/found/made (by the "
    "bot, or just 'someone'). Reacting to a meme, discussing memes, or "
    "narrating plans ('I'll send a meme later') is NOT a request.\n\n"
    "Message: {text}\n\n"
    "Reply with EXACTLY one line:\n"
    "NO            — not a meme request\n"
    "THIS          — asking for a meme about the current conversation "
    "('this', 'that', no subject given)\n"
    "TOPIC: <subject> — asking for a meme about a specific subject"
)

MEME_PICK_PROMPT = (
    "You are choosing a MEME for a group chat.\n"
    "Topic: {topic}\n\n"
    "Candidates (upvotes in parentheses when known, flair in [brackets]):\n"
    "{candidates}\n\n"
    "Pick the candidate most likely to actually get a laugh:\n"
    "  1. It MUST be an actual meme — joke/image-macro/shitpost format. "
    "NOT a news photo, article screenshot, game highlight, product shot, "
    "or ordinary picture. Meme subreddits and flairs like [Meme]/"
    "[Shitpost] are strong signals; sober titles that read like headlines "
    "are not memes.\n"
    "  2. LOOSE topical relevance is enough. A heavily-upvoted, genuinely "
    "funny meme that's only loosely related BEATS a precisely on-topic "
    "meme with few upvotes. Upvotes are the crowd's verdict on funny — "
    "weigh them heavily.\n"
    "Reply with ONLY its number. Reply 0 only if nothing is both a meme "
    "and at least loosely related to the topic."
)

HAIKU_PROMPT = (
    "Compose a single haiku (5-7-5 syllables, three lines) inspired by the "
    "chat snippet below. Be evocative, a little weird, in character as a "
    "wry observer. Output ONLY the haiku — three lines, no preamble.\n\n"
    "Chat:\n{messages}"
)

THIS_OR_THAT_PROMPT = (
    "Decide between A and B. One short paragraph (under 60 words). Don't "
    "hedge — name the winner first, then justify with absurd or surprisingly "
    "specific reasoning. End with a flourish.\n\n"
    "A: {a}\nB: {b}"
)

ECHO_PROMPT = (
    "Below are recent messages from {name}. Mimic their style — their tone, "
    "vocabulary, capitalization habits, punctuation, sentence length, "
    "specific quirks. Write ONE short message they might plausibly send "
    "about: {topic}. Output ONLY that message, no preamble, no quotes.\n\n"
    "Examples from {name}:\n{messages}"
)

ROAST_PROMPT = (
    "Roast {name} in 1-3 sentences. Punch up, be playful, never cruel or "
    "personal. Base it on these recent messages. Output ONLY the roast.\n\n"
    "Recent {name}:\n{messages}"
)

COMPLIMENT_PROMPT = (
    "Compliment {name} in 1-3 sentences. Be sincere but specific — quote "
    "back something good you noticed in their recent messages. Output "
    "ONLY the compliment.\n\n"
    "Recent {name}:\n{messages}"
)

YEAR_RETRO_PROMPT = (
    "Below is a year of chat highlights (compressed). Write a fond, "
    "slightly exaggerated 'Year in Review' for this group. 6-10 bullets. "
    "Include running jokes, recurring characters, the most ridiculous "
    "moments, in-jokes. Output the bullets only.\n\n"
    "Year:\n{messages}"
)

TLDR_PROMPT = (
    "TL;DR of the recent chat below. 3-7 bullet points, in chronological "
    "order, third-person, no preamble, drop fluff, keep names. Output "
    "only the bullets.\n\nMessages:\n{messages}"
)

FACT_EXTRACT_PROMPT = (
    "Read the chat snippet and extract durable facts worth remembering "
    "long-term. Include:\n"
    "- preferences (foods, drinks, music, hobbies, pets, sports teams)\n"
    "- relationships (who knows whom, partners, friends, family, pets)\n"
    "- recurring jokes or in-references that define this chat\n"
    "- ongoing situations (jobs, projects, moves, health, travel)\n"
    "- self-statements ('I work at X', 'I'm allergic to peanuts',\n"
    "  'I live in Portland')\n"
    "\n"
    "Skip pure pleasantries ('hi', 'lol', 'thx'), one-off jokes that don't "
    "recur, and clearly uncertain claims. Output one fact per line, in "
    "'subject — fact' form when possible:\n"
    "  Matt — drinks White Russians\n"
    "  Pedro — afraid of nihilists\n"
    "  this chat — Thursday trivia nights are a recurring tradition\n"
    "\n"
    "Lean toward extracting SOMETHING rather than nothing. Only output the "
    "single word NONE if the snippet is genuinely all small talk.\n"
    "\n"
    "Chat snippet:\n{messages}"
)
