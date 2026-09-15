"""Guard behaviour for the web image fetcher (no network in tests)."""

from __future__ import annotations

import pytest

from ipedro.quizzes import image_fetch


@pytest.mark.asyncio
async def test_blank_query_returns_none_without_touching_the_network():
    assert await image_fetch.fetch("") is None
    assert await image_fetch.fetch("   ") is None
