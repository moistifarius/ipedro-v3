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
    "Generate a short Telegram-friendly ASCII/emoji art of a happy little duck "
    "saying 'quack'. Output ONLY the art - no preface, no explanation."
)

# Bef decision: the AI gates a successful dice roll. It MUST reply with one
# line starting with either ACCEPT: or REFUSE: followed by a punchy in-chat
# message (the chat-facing line itself). Rarity is included so the duck's
# personality scales: common ducks are friendlier, legendary ducks are
# imperious and refuse for ridiculous reasons.
DUCK_BEF_DECIDE_PROMPT = (
    "You play a duck in a chat game. A player named {display_name} just "
    "tried to befriend you. You are a {rarity} duck. Higher-rarity ducks are "
    "snootier, more capricious, and more likely to refuse for absurd reasons. "
    "The player has befriended {friend_count} duck(s) before in this chat.\n\n"
    "Decide if you, the duck, want to be friends RIGHT NOW. Be chaotic. "
    "Common ducks usually agree. Rare ducks are picky. Legendary ducks "
    "almost always refuse and are dramatic about it.\n\n"
    "Reply with EXACTLY ONE line, no preamble:\n"
    "ACCEPT: <one short punchy second-person line - the duck warming to them>\n"
    "OR\n"
    "REFUSE: <one short punchy second-person line - the duck declining, in-character for the rarity>\n"
    "Do NOT include any other text."
)

# Bef retry challenge: the previous bef attempt was refused. The user must
# solve one of three kinds of challenge before trying again. Kind is chosen
# by the service; the prompt picks the actual content.
DUCK_BEF_CHALLENGE_PROMPT = (
    "Generate a single small challenge for {display_name} to solve in a "
    "Telegram chat game. The challenge type is: {kind}.\n"
    "- captcha: ask them to spell out a specific short word or phrase using "
    "  emoji, punctuation, or weirdly-spaced letters. Make it possible.\n"
    "- trivia: ask one weird, off-the-wall trivia question. It can be obscure "
    "  but must have a roughly real answer.\n"
    "- recipe: ask them to type out a brief recipe for something specific "
    "  (e.g. 'write me a recipe for a duck-themed sandwich').\n\n"
    "Output ONLY the challenge text - no preamble, no 'here is your challenge', "
    "no explanation. Keep it under 280 characters. The user will reply to "
    "the message with their attempt."
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
    "Existing summary (may be empty):\n{prior}\n\nNew messages:\n{messages}\n"
)

FACT_EXTRACT_PROMPT = (
    "Read the chat snippet. Extract at most 3 high-signal facts worth remembering "
    "long-term about specific users or this chat (e.g. 'Alice is a vegetarian'). "
    "Skip ephemeral chatter, jokes, and uncertain claims. Output one fact per "
    "line, or output the single word NONE if nothing qualifies.\n\n"
    "Chat snippet:\n{messages}"
)
