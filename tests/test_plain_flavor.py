"""A bot that isn't Dale never says his catchphrases.

Dale's canned lines ("Sh-sha. Negative contact.", "Pocket sand! ...sorry,
reflex.") are written into duckhunt, the meme hunt, /onthisday and more. A
/newbot bot used to recite them. Plain bots now run everything they send
through identity.plainify; these tests keep every canned line strippable,
including ones added later.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiogram.methods import SendMessage, SendPhoto

from ipedro import bot as bot_mod
from ipedro.identity import plainify
from ipedro.plain_flavor import PlainFlavorMiddleware

ROOT = Path(__file__).resolve().parent.parent / "ipedro"
CATCHPHRASE = re.compile(r"sh+-?sha+|pocket\s+sand", re.IGNORECASE)

# Dale-only on purpose: his GIF/automod tables and persona, his own lines
# (gated on dale_flavor in chat.py), his help text. Nothing here reaches a
# plain bot.
DALE_ONLY = {
    "handlers/automod_bits.py", "dale_gif_seeds.py", "identity.py", "personas.py",
    "handlers/basics.py", "prompts.py", "dale_gifs.py", "handlers/dale.py",
}


def _string_literals(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def test_every_canned_line_in_the_code_can_be_made_plain():
    """Walk every string literal in the package. Any that holds a Dale
    catchphrase must come out of plainify without it. The literals that
    only exist for Dale are exempt (they're gated at the source)."""
    offenders = []
    checked = 0
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in DALE_ONLY:
            continue
        for text in _string_literals(path):
            if not CATCHPHRASE.search(text):
                continue
            if rel == "handlers/chat.py" and text.islower():
                continue          # the credit/thanks pools: gated on dale_flavor
            checked += 1
            if CATCHPHRASE.search(plainify(text)):
                offenders.append((rel, text[:80]))
    assert checked >= 20, "the scan stopped finding the canned lines"
    assert not offenders, offenders


@pytest.mark.parametrize("line,plain", [
    ("Sh-sha. Friend confirmed.", "Friend confirmed."),
    ("Sh-sha! Shooting at a phantom. That's how they get you.",
     "Shooting at a phantom. That's how they get you."),
    ("Pocket sand! ...sorry, reflex. No duck.", "No duck."),
    ("Pocket sand! ...sorry. Wait a beat.", "Wait a beat."),
    ("⏱ Too slow. Sh-sha. You were looking that up.",
     "⏱ Too slow. You were looking that up."),
    ("Nothing to change here.", "Nothing to change here."),
])
def test_plainify(line, plain):
    assert plainify(line) == plain


def test_a_line_that_is_only_the_catchphrase_is_not_blanked():
    assert plainify("Sh-sha.") == "Sh-sha."


# ── on the wire ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_middleware_cleans_text_and_captions_and_nothing_else():
    seen = []

    async def make_request(bot, method):
        seen.append(method)
        return "ok"

    mw = PlainFlavorMiddleware()
    msg = SendMessage(chat_id=-5, text="Sh-sha. Slow down.", reply_to_message_id=3)
    await mw(make_request, None, msg)
    pic = SendPhoto(chat_id=-5, photo="file", caption="Pocket sand! ...sorry. Look.")
    await mw(make_request, None, pic)
    clean = SendMessage(chat_id=-5, text="hello")
    await mw(make_request, None, clean)

    assert seen[0].text == "Slow down."
    assert seen[0].chat_id == -5 and seen[0].reply_to_message_id == 3
    assert seen[1].caption == "Look." and seen[1].photo == "file"
    assert seen[2] is clean                      # untouched: no copy made
    assert msg.text == "Sh-sha. Slow down."      # the original isn't mutated


def _settings(**kw):
    base = dict(bot_name="Dale", bot_aliases="", bot_flavor="dale")
    base.update(kw)
    return SimpleNamespace(**base)


def test_only_a_plain_bot_gets_the_filter():
    dale = SimpleNamespace(session=MagicMock())
    bot_mod.install_flavor(dale, _settings())
    dale.session.middleware.assert_not_called()

    hank = SimpleNamespace(session=MagicMock())
    bot_mod.install_flavor(hank, _settings(bot_name="Hank", bot_flavor="plain"))
    hank.session.middleware.assert_called_once()
    assert isinstance(hank.session.middleware.call_args.args[0], PlainFlavorMiddleware)


@pytest.mark.asyncio
async def test_the_share_photo_loop_does_not_run_for_a_bot_that_isnt_dale():
    """Its scene, render and caption prompts are the Dude's. Switched on in a
    chat, a plain bot would post Lebowski-voiced photos over its own persona."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ipedro import sharephoto

    stop = asyncio.Event()
    db = SimpleNamespace(fetch=AsyncMock(return_value=[]))
    plain = SimpleNamespace(bot_name="Hank", bot_aliases="hank", bot_flavor="plain",
                            share_photo_tick_seconds=1, share_photo_mean_interval_seconds=1)
    await asyncio.wait_for(
        sharephoto.run_share_photo_loop(None, db, None, plain, stop), timeout=1)
    db.fetch.assert_not_awaited()                     # returned at once, never looked
