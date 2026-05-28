"""Pinning tests for the rewritten FACT_EXTRACT_PROMPT (Fix 3) and the
random-challenge pool (Fix 4).

These are intentionally small smoke tests — they don't try to validate
LLM output, they just lock in the intent of the change so future edits
don't accidentally drop the new keywords or re-enable recipe in the
random pool.
"""

from __future__ import annotations

import random

from ipedro.handlers.duckhunt import (
    _CHALLENGE_KINDS, _RANDOM_CHALLENGE_KINDS,
)
from ipedro.prompts import FACT_EXTRACT_PROMPT


def test_fact_extract_prompt_includes_new_keywords():
    """Pin the rewrite: the prompt should explicitly mention preferences,
    relationships, self-statements, and the 'lean toward extracting' nudge.
    The old prompt was too conservative and returned NONE in DMs."""
    prompt_lower = FACT_EXTRACT_PROMPT.lower()
    # Core category words from the rewrite.
    for keyword in (
        "preferences", "relationships", "recurring jokes",
        "self-statements",
    ):
        assert keyword in prompt_lower, (
            f"FACT_EXTRACT_PROMPT missing {keyword!r}: "
            "did Fix 3 get reverted?"
        )
    # The bias toward extracting something (the heart of Fix 3).
    assert "lean toward extracting" in prompt_lower or (
        "rather than nothing" in prompt_lower
    ), "FACT_EXTRACT_PROMPT lost its extract-bias nudge"
    # NONE is still the explicit empty-output sentinel.
    assert "NONE" in FACT_EXTRACT_PROMPT


def test_fact_extract_prompt_has_subject_dash_fact_examples():
    """Output format examples ('subject — fact') help the LLM produce
    cleanly-stripable lines. Pin that the rewrite kept them."""
    # The em-dash is the load-bearing format separator.
    assert "—" in FACT_EXTRACT_PROMPT
    # At least one concrete example to anchor the format.
    assert "Matt" in FACT_EXTRACT_PROMPT or "drinks" in FACT_EXTRACT_PROMPT


def test_random_challenge_excludes_recipe():
    """Recipe is currently disabled in the random selection pool but
    still issuable via /debug_recipe. The random tuple should not
    contain recipe."""
    assert "recipe" in _CHALLENGE_KINDS  # still allowed in force_kind
    assert "recipe" not in _RANDOM_CHALLENGE_KINDS
    # Captcha and trivia should still both be available randomly.
    assert "captcha" in _RANDOM_CHALLENGE_KINDS
    assert "trivia" in _RANDOM_CHALLENGE_KINDS


def test_random_choice_over_pool_never_picks_recipe():
    """Stronger version: actually call random.choice many times and
    confirm 'recipe' never surfaces. Guards against the pool being
    silently re-expanded by an edit."""
    rng = random.Random(0)
    for _ in range(500):
        kind = rng.choice(_RANDOM_CHALLENGE_KINDS)
        assert kind != "recipe", (
            "random.choice picked 'recipe' — pool was re-expanded?"
        )
