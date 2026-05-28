"""Image captcha generation for bef challenges.

The captcha library renders distorted text as PNG bytes. We pick from an
alphabet that omits visually-ambiguous characters (0/O, 1/I/L) so the
judge can do a strict case-insensitive compare without false rejects.
"""

from __future__ import annotations

import random

from captcha.image import ImageCaptcha

_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_IMG = ImageCaptcha(width=240, height=90)


def make_captcha(length: int = 5) -> tuple[str, bytes]:
    """Return (answer, png_bytes). Answer is normalized to uppercase."""
    answer = "".join(random.choice(_ALPHABET) for _ in range(length))
    return answer, _IMG.generate(answer).getvalue()


def matches(expected: str, attempt: str) -> bool:
    norm = lambda s: "".join(ch for ch in s.upper() if ch.isalnum())
    return bool(expected) and norm(expected) == norm(attempt)
