"""The pinned starter set of Dale GIFs: data only, no logic.

Kept apart from dale_gifs.py so a change to the table alone counts as
plain content under ipedro/merge_policy.py (an /evolve request to add a
GIF can merge without the owner looking), while the code that downloads
and sends these — and SEED_HOSTS, which decides what hosts are
acceptable at all — stays where every change waits for review.

A URL here on a host outside dale_gifs.SEED_HOSTS is refused at seed
time, not just flagged by a test.
"""

from __future__ import annotations

# Every URL below was downloaded and looked at before it was pinned, across
# two passes. 18 candidates were dropped in the process: duplicate scenes at
# different sizes, one that was Bill and Hank with no Dale in frame, a couple
# of bland group shots, one that wasn't King of the Hill at all, and SIX
# separate uploads of the same anonymous hand-in-a-pocket close-up, which is
# what most of Tenor's "pocket sand" results actually are.
SEED_GIFS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # -- the signature bit --
    ("https://media.tenor.com/5rV2htsmYroAAAAM/pocket-sand-dale-gribble.gif",
     ("pocketsand",)),
    ("https://media.tenor.com/8xOtgiVgw0gAAAAM/dale-gribble-koth.gif",
     ("shsha",)),

    # -- conspiracy / paranoia --
    ("https://media.tenor.com/gDz9uTO2Gk4AAAAM/"
     "my-suspicions-have-been-confirmed-dale-gribble.gif",
     ("conspiracy", "paranoia")),
    ("https://media.tenor.com/fSgocjK3KDQAAAAM/"
     "i-suspected-since-day-since-one-dale-gribble.gif",
     ("conspiracy", "paranoia")),
    ("https://media.tenor.com/agbpcYxrKPkAAAAM/think-about-it-dale-gribble.gif",
     ("conspiracy", "paranoia")),
    # "...the global information conspiracy otherwise known as 'the Beast'"
    ("https://media.tenor.com/4Meyn03YnRIAAAAM/king-of-the-hill-dale-gribble.gif",
     ("conspiracy", "paranoia")),
    # "Guns don't kill people, the government does."
    ("https://media.tenor.com/F3KCHf1DEO4AAAAM/king-of.gif",
     ("conspiracy", "guns")),
    ("https://media.tenor.com/KO_F34Bqe78AAAAM/king-of-the-hill-hank-hill.gif",
     ("paranoia",)),

    # -- smug / the expert --
    ("https://media.tenor.com/L_8WRznhLaEAAAAM/ha-gotcha.gif",
     ("smug", "shsha")),
    ("https://media.tenor.com/fC5qIgBvW9YAAAAM/"
     "are-you-familiar-with-my-credentials-credentials.gif",
     ("smug", "expert", "shsha")),
    ("https://media.tenor.com/EXXyadd0J9EAAAAM/"
     "you-called-the-right-guy-dale-gribble.gif",
     ("smug", "expert")),

    # -- agreement --
    ("https://media.tenor.com/PIhbe0SLtkAAAAAM/noted-dale-gribble.gif",
     ("agree", "shsha")),
    ("https://media.tenor.com/SJUZyeEIc3MAAAAM/yep-dale-gribble.gif", ("agree",)),
    ("https://media.tenor.com/Iu1fx7hN7tAAAAAM/yup-dale-gribble.gif", ("agree",)),

    # -- "I'm skeptical that you could, yet intrigued that you may." --
    ("https://media.tenor.com/_5Jn7fS-ASEAAAAM/king-of-the-hill-dale-gribble.gif",
     ("doubt",)),

    # -- guns --
    ("https://media.tenor.com/gcKoCR72Iu4AAAAM/dale-gribble-king-of-the-hill.gif",
     ("guns",)),
    ("https://media.tenor.com/2RNa9iGu1gcAAAAM/reload-shotgun.gif",
     ("guns", "paranoia")),
    ("https://media.tenor.com/oa0rInOgM2QAAAAM/dale-gribble-king-of-the-hill.gif",
     ("guns", "paranoia")),

    # -- panic / flight --
    ("https://media.tenor.com/Gs_sT0J_cZkAAAAM/scared-scared-face.gif",
     ("panic",)),
    ("https://media.tenor.com/sMjbjZcAgusAAAAM/vertigo-dale-gribble.gif",
     ("panic",)),
    ("https://media.tenor.com/VamCz7JBHMkAAAAM/king-of-the-hill-dale-gribble.gif",
     ("panic", "chaos")),
    ("https://media.tenor.com/fBbBwLO2yjkAAAAM/dale-gribble-king-of-the-hill.gif",
     ("panic", "flee")),          # "Squirrel Tactic!"
    ("https://media.tenor.com/My3ExpNPJy4AAAAM/dale-gribble-dale.gif",
     ("flee",)),                  # "So long, suckers"

    # -- deadpan filler, mostly for the ambient sprinkle --
    ("https://media.tenor.com/z_7eIX8X06IAAAAM/king-of-the-hill-dale-gribble.gif",
     ("deadpan",)),
    ("https://media.tenor.com/oLlX7pYScZIAAAAM/king-of-the-hill-dale-gribble.gif",
     ("deadpan",)),               # "You wouldn't hit an unconscious man."

    # -- second pass: filling the buckets that had only one GIF, so the
    # -- signature bits stop repeating the same clip every time --
    ("https://media.tenor.com/ASoyuGdMMm8AAAAM/pocket-sand-dale.gif",
     ("pocketsand",)),            # the bar scene, Dale's face in frame
    ("https://media.tenor.com/Mlb73N-O7WYAAAAM/dale-gribble-squirrel-tactic.gif",
     ("flee", "panic")),          # wider cut of "Squirrel Tactic!"
    ("https://media.tenor.com/eDo0vuCTHYgAAAAM/king-of-the-hill-dale-gribble.gif",
     ("panic", "chaos")),
    ("https://media.tenor.com/HaDGdBi3DZ4AAAAM/nod-nodding.gif",
     ("deadpan", "agree")),
    ("https://media.tenor.com/Eh4lPv9GXxQAAAAM/stand-off-snake.gif",
     ("deadpan",)),
    ("https://media.tenor.com/xvf3XrrPlNwAAAAM/king-of-the-hill-koth.gif",
     ("deadpan",)),               # the alley, beers, with Boomhauer
)
