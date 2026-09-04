"""Prompt caching: the stable prefix, the breakpoint, and the accounting.

Caching fails silently. The request still succeeds, the reply is still
good, the bill is just higher — no error, nothing announces it. And the
usual shape is a regression rather than a bad first build: it works when
written, then someone adds a field to the system prompt and every request
misses from then on.

So the load-bearing test here is the byte-identity one: two requests a few
minutes apart in the same chat must produce the SAME stable prefix bytes.
Everything else is scaffolding around that.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ipedro.capabilities import capability_brief
from ipedro.config import Settings
from ipedro.db.repositories import (
    ChatConfig, StoredFact, StoredMessage, StoredSummary,
)
from ipedro.memory.context_builder import build_context
from ipedro.memory.tokens import count_tokens
from ipedro.openai_client import (
    CACHE_BREAKPOINT, _cache_minimum, _claude_text_price,
    _normalize_for_claude,
)

NOW = datetime(2026, 9, 4, 14, 47, tzinfo=timezone.utc)


class _Store:
    """Enough history that the prefix clears Sonnet's 1024-token minimum."""

    def __init__(self, *, summary=True, facts=20):
        self._summary, self._facts = summary, facts

    async def recent_messages(self, chat_id, limit):
        return [
            StoredMessage(
                id=i, chat_id=1, message_id=i, user_id=7, role="user",
                content=f"message number {i} about propane",
                tokens=None, created_at=NOW - timedelta(minutes=30 - i),
                author_name="Matt",
            )
            for i in range(1, 6)
        ]

    async def latest_summary(self, chat_id):
        if not self._summary:
            return None
        return StoredSummary(
            id=1, chat_id=1, summary="- the group argues about propane\n" * 6,
            covers_until_id=99, created_at=NOW,
        )

    async def list_facts(self, chat_id, limit=50):
        return [
            StoredFact(id=i, chat_id=1, user_id=7,
                       fact=f"Matt likes thing number {i}",
                       source_msg=None, created_at=NOW)
            for i in range(self._facts)
        ]

    async def semantic_search(self, chat_id, query, k=6):
        # Keyed on THIS message, so it must land in the volatile half.
        return [{
            "ref_kind": "message", "ref_id": 1, "similarity": 0.5,
            "content": f"an earlier remark about {query}", "author_name": "Luke",
        }]


def _settings():
    return Settings(  # type: ignore[call-arg]
        telegram_bot_token="t", openai_api_key="k",
        database_url="postgresql://t/t",
        context_recent_messages=10, context_max_tokens=10_000,
    )


def _cfg():
    return ChatConfig(
        chat_id=1, response_policy="mention", ambient_probability=0.03,
        persona="dude", persona_custom=None, duckhunt_enabled=True,
        voice_transcribe=True, memory_enabled=True,
    )


async def _ctx(text="what about propane", *, when=NOW, store=None, **kw):
    opts = dict(
        store=store or _Store(), settings=_settings(), chat_id=1,
        persona="dude", persona_custom=None, latest_user_text=text,
        latest_user_name="Matt", capabilities=capability_brief(_cfg()),
        extra_system="You are in a CRANKY mood right now.", now=when,
    )
    opts.update(kw)
    return await build_context(**opts)


def _split(ctx, model="claude-sonnet-4-6"):
    return _normalize_for_claude(ctx.messages, model=model)


# ── the invariant everything else rests on ───────────────────────────────────

@pytest.mark.asyncio
async def test_the_stable_prefix_is_byte_identical_between_messages():
    """THE test. Two different messages, five minutes apart, in the same
    chat: the cached half must not differ by a single byte, or every
    request is a cache miss and the whole feature is an expensive no-op."""
    a = await _ctx("what about propane", when=NOW)
    b = await _ctx("and the grill", when=NOW + timedelta(minutes=5))

    sys_a, _ = _split(a)
    sys_b, _ = _split(b)

    assert isinstance(sys_a, list) and isinstance(sys_b, list)
    assert sys_a[0]["text"] == sys_b[0]["text"], (
        "stable prefix drifted between requests — cache will never hit"
    )
    # …and the volatile halves genuinely differ, so the split is real and
    # not just everything landing on the stable side.
    assert sys_a[1]["text"] != sys_b[1]["text"]


@pytest.mark.asyncio
async def test_the_clock_is_not_in_the_cached_prefix():
    """A timestamp in the prefix is the textbook silent invalidator: it
    changes every minute and re-bills everything behind it."""
    stable, _ = _split(await _ctx())
    # The STAMP is volatile; the sentence explaining how to read it is
    # stable and rides in the prefix (it quotes the phrase, which is fine).
    assert "September 2026" not in stable[0]["text"]
    assert "Right now it is Friday, 4 September 2026" in stable[1]["text"]


@pytest.mark.asyncio
async def test_the_clock_explainer_is_cached_and_the_stamp_is_not():
    """~75 tokens of 'how to read the time and the gap markers' never
    change; only the stamp does. Splitting them moves the explainer to a
    tenth of the price on every reply."""
    stable, _ = _split(await _ctx())
    cached, volatile = stable[0]["text"], stable[1]["text"]
    assert "Inline markers like" in cached and "Inline markers like" not in volatile
    assert volatile.count("Right now it is") == 1


@pytest.mark.asyncio
async def test_per_request_content_lands_after_the_breakpoint():
    """Semantic hits are keyed on this message and the mood changes on its
    own schedule; both are volatile by construction."""
    stable, _ = _split(await _ctx("propane"))
    cached, volatile = stable[0]["text"], stable[1]["text"]
    assert "an earlier remark" in volatile and "an earlier remark" not in cached
    assert "CRANKY" in volatile and "CRANKY" not in cached


@pytest.mark.asyncio
async def test_the_expensive_stable_blocks_are_inside_the_prefix():
    """The capability brief is ~1000 tokens on every reply — it is the
    single biggest thing caching is here to pay for."""
    stable, _ = _split(await _ctx())
    cached = stable[0]["text"]
    assert "What you can and can't do" in cached      # capability brief
    assert "burst and friction" in cached             # style rule
    assert "display name" in cached                   # name-prefix rule
    assert "Conversation summary so far" in cached
    assert "Known durable facts" in cached


@pytest.mark.asyncio
async def test_the_breakpoint_marks_the_last_stable_message():
    ctx = await _ctx()
    marked = [i for i, m in enumerate(ctx.messages) if m.get(CACHE_BREAKPOINT)]
    assert len(marked) == 1, "exactly one breakpoint, on the boundary"
    after = ctx.messages[marked[0] + 1]
    assert "Right now it is" in after["content"], (
        "the block after the breakpoint should be the first volatile one"
    )


# ── the minimum-prefix guard ─────────────────────────────────────────────────

def test_cache_minimums_are_looked_up_not_guessed():
    """The minimum is NOT monotonic across generations — a prefix that
    caches on Sonnet silently won't on Haiku."""
    assert _cache_minimum("claude-sonnet-4-6") == 1024
    assert _cache_minimum("claude-haiku-4-5") == 4096
    assert _cache_minimum("claude-opus-5") == 512
    # An unknown future model assumes the strictest minimum rather than
    # emitting a marker that silently does nothing.
    assert _cache_minimum("claude-something-unreleased") == 4096


@pytest.mark.asyncio
async def test_no_marker_when_the_prefix_is_below_the_model_minimum():
    """Haiku needs 4096 tokens; our prefix is ~1400. Sending a marker it
    will decline is decoration, so we send the plain string instead."""
    ctx = await _ctx()
    sys_haiku, _ = _split(ctx, model="claude-haiku-4-5")
    assert isinstance(sys_haiku, str)
    # Nothing is lost — every block is still in the prompt, just unsplit.
    assert "What you can and can't do" in sys_haiku
    assert "Right now it is" in sys_haiku


def test_an_unmarked_message_array_keeps_the_old_joined_string():
    """cheap_chat and the one-off prompts pass no breakpoint; they must
    behave exactly as before."""
    sys_out, chat = _normalize_for_claude(
        [{"role": "system", "content": "be terse"},
         {"role": "user", "content": "hi"}],
        model="claude-sonnet-4-6",
    )
    assert sys_out == "be terse"
    assert chat == [{"role": "user", "content": "hi"}]


# ── the normalizer's existing contracts still hold ───────────────────────────

def test_splitting_preserves_the_claude_message_rules():
    """Consecutive same-role merging, no leading assistant, never empty."""
    sys_out, chat = _normalize_for_claude([
        {"role": "assistant", "content": "stray leading turn"},
        {"role": "system", "content": "A" * 40, CACHE_BREAKPOINT: True},
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
    ], model="claude-sonnet-4-6")
    assert chat == [{"role": "user", "content": "one\n\ntwo"}]
    assert isinstance(sys_out, str)      # below the minimum → plain string


def test_a_marked_array_with_no_chat_still_synthesizes_a_user_turn():
    _, chat = _normalize_for_claude(
        [{"role": "system", "content": "x", CACHE_BREAKPOINT: True}],
        model="claude-sonnet-4-6",
    )
    assert chat == [{"role": "user", "content": "(continue)"}]


def test_volatile_half_is_omitted_when_there_is_nothing_volatile():
    big = "word " * 3000
    blocks, _ = _normalize_for_claude(
        [{"role": "system", "content": big, CACHE_BREAKPOINT: True},
         {"role": "user", "content": "hi"}],
        model="claude-sonnet-4-6",
    )
    assert isinstance(blocks, list) and len(blocks) == 1
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


# ── cost accounting ──────────────────────────────────────────────────────────

def test_cached_tokens_are_priced_separately():
    """input_tokens EXCLUDES cached tokens. Pricing only that field means
    the cost report drops most of the bill exactly when caching works."""
    fresh = _claude_text_price("claude-sonnet-4-6", 2000, 200)
    # Same total prompt, but 1500 of it served from cache.
    cached = _claude_text_price("claude-sonnet-4-6", 500, 200,
                                cache_read_tokens=1500)
    assert cached < fresh
    # A read is a tenth of base input, so the saving is 90% of that slice.
    saved = (1500 * 0.003 / 1000) * 0.9
    assert abs((fresh - cached) - saved) < 1e-9


def test_a_cache_write_costs_more_than_not_caching():
    """The write premium is real; it has to show up or the break-even
    arithmetic in the docstring is a lie."""
    plain = _claude_text_price("claude-sonnet-4-6", 2000, 0)
    write = _claude_text_price("claude-sonnet-4-6", 500, 0,
                               cache_write_tokens=1500)
    assert write > plain
    # …and two hits more than pay it back.
    read = _claude_text_price("claude-sonnet-4-6", 500, 0,
                              cache_read_tokens=1500)
    assert write + 2 * read < 3 * plain


def test_pricing_without_cache_fields_is_unchanged():
    """Every non-Claude-chat call site still passes two args."""
    assert _claude_text_price("claude-sonnet-4-6", 1000, 1000) == pytest.approx(
        (1000 / 1000) * 0.003 + (1000 / 1000) * 0.015
    )


# ── the win, stated in numbers ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_most_of_the_prompt_is_actually_cacheable():
    """If the split ever inverts — most of the prompt volatile, a sliver
    cached — caching stops being worth its own complexity."""
    stable, _ = _split(await _ctx())
    cached_t = count_tokens(stable[0]["text"])
    volatile_t = count_tokens(stable[1]["text"])
    assert cached_t > 1024, "below Sonnet's minimum; nothing would cache"
    assert cached_t > 3 * volatile_t, (
        f"only {cached_t} cached vs {volatile_t} volatile — split has drifted"
    )


@pytest.mark.asyncio
async def test_the_clock_survives_at_the_production_budget():
    """Reordering moved summary+facts ahead of the clock in the emission
    order, so they now compete for the budget first. At the real default
    (6000) there must still be room — a bot that loses track of the time
    because the summary grew would be a silent regression."""
    settings = _settings()
    settings.context_max_tokens = 6000
    ctx = await _ctx(settings=settings)
    system = "\n".join(m["content"] for m in ctx.messages if m["role"] == "system")
    assert "Right now it is" in system
    assert ctx.tokens <= 6000


# ── the sampling gate: what makes a model switch possible at all ─────────────

def test_the_newer_generation_rejects_sampling_parameters():
    """Sending temperature to any of these is a 400 on every request. The
    gate used to name Opus 4.7 alone, so pointing /ai_model at any other
    current model would have bricked the bot."""
    from ipedro.openai_client import _rejects_sampling

    for model in ("claude-sonnet-5", "claude-opus-5", "claude-opus-4-8",
                  "claude-opus-4-7", "claude-fable-5-1"):
        assert _rejects_sampling(model), model
    # The models this bot runs today still take temperature.
    for model in ("claude-sonnet-4-6", "claude-haiku-4-5"):
        assert not _rejects_sampling(model), model


def test_the_default_model_is_the_cheaper_newer_sonnet():
    """Sonnet 5 is $2/$10 per MTok against Sonnet 4.6's $3/$15 — 33% off
    both directions, compounding with the cache. Pinned so a stray env
    default can't quietly walk it back."""
    from ipedro.config import Settings
    from ipedro.openai_client import OpenAIClient

    s = Settings(telegram_bot_token="t", openai_api_key="k",  # type: ignore[call-arg]
                 database_url="postgresql://t/t")
    assert s.claude_text_model == "claude-sonnet-5"
    assert OpenAIClient(api_key="x").claude_model == "claude-sonnet-5"
    assert _claude_text_price("claude-sonnet-5", 2000, 100) < (
        _claude_text_price("claude-sonnet-4-6", 2000, 100)
    )


def test_every_selectable_model_has_a_price_row():
    """A missing row falls back to Sonnet's rate, which would quietly
    misreport the bill for a model in a different tier."""
    from ipedro.handlers.admin import _KNOWN_CLAUDE_TEXT_MODELS
    from ipedro.openai_client import _CLAUDE_TEXT_PRICE_PER_1K

    priced = tuple(_CLAUDE_TEXT_PRICE_PER_1K)
    for model in _KNOWN_CLAUDE_TEXT_MODELS:
        assert model.startswith(priced), f"{model} has no price row"


def test_the_default_model_is_selectable_and_cacheable():
    from ipedro.handlers.admin import _KNOWN_CLAUDE_TEXT_MODELS
    from ipedro.openai_client import OpenAIClient

    default = OpenAIClient(api_key="x").claude_model
    assert default in _KNOWN_CLAUDE_TEXT_MODELS
    assert _cache_minimum(default) <= 1024      # our ~1450-token prefix caches


# ── thinking: off for a chat bot, on the models that would otherwise think ──

@pytest.mark.asyncio
async def test_thinking_is_disabled_on_models_that_think_by_default():
    """Omitting `thinking` on Sonnet 5 runs adaptive thinking: reasoning
    tokens billed as output and counted against the 500-token reply cap.
    For two-sentence persona replies that is cost and truncation for
    nothing."""
    from ipedro.openai_client import OpenAIClient

    captured: dict = {}

    class _Msgs:
        async def create(self, **kw):
            captured.update(kw)
            return type("R", (), {"content": [], "usage": None})()

    client = OpenAIClient(api_key=None, anthropic_api_key="k",
                          claude_model="claude-sonnet-5")
    client._anthropic = type("A", (), {"messages": _Msgs()})()
    await client.chat([{"role": "user", "content": "hi"}])
    assert captured["thinking"] == {"type": "disabled"}
    assert "temperature" not in captured        # rejected by this generation


@pytest.mark.asyncio
async def test_older_models_are_left_exactly_as_before():
    """Sonnet 4.6 / Haiku 4.5 run without thinking when it is omitted and
    still take temperature — sending a thinking block there would be a
    behaviour change for no reason."""
    from ipedro.openai_client import OpenAIClient

    for model in ("claude-sonnet-4-6", "claude-haiku-4-5"):
        captured: dict = {}

        class _Msgs:
            async def create(self, **kw):
                captured.update(kw)
                return type("R", (), {"content": [], "usage": None})()

        client = OpenAIClient(api_key=None, anthropic_api_key="k",
                              claude_model=model)
        client._anthropic = type("A", (), {"messages": _Msgs()})()
        await client.chat([{"role": "user", "content": "hi"}])
        assert "thinking" not in captured, model
        assert captured["temperature"] == 1.0, model


# ── slow-moving caller context rides in the prefix ───────────────────────────

@pytest.mark.asyncio
async def test_stable_extra_is_cached_and_extra_system_is_not():
    """Mood and word-of-the-day hold still for hours; the snark flag is
    per message. Same mechanism, opposite sides of the breakpoint."""
    ctx = await _ctx(
        stable_extra="You are in a SMUG mood right now.",
        extra_system="The user you're replying to is on your shit list.",
    )
    cached, volatile = (b["text"] for b in _split(ctx)[0])
    assert "SMUG" in cached and "SMUG" not in volatile
    assert "shit list" in volatile and "shit list" not in cached


# ── retrieval never pays for what is already in the request ──────────────────

class _EchoStore(_Store):
    """Retrieval that returns the message being answered, a fact already in
    the prefix, and one genuinely new recollection."""

    async def semantic_search(self, chat_id, query, k=6):
        return [
            {"ref_kind": "message", "ref_id": 99, "similarity": 1.0,
             "content": query, "author_name": "Matt"},
            {"ref_kind": "fact", "ref_id": 3, "similarity": 0.6,
             "content": "Matt likes thing number 3", "author_name": None},
            {"ref_kind": "message", "ref_id": 7, "similarity": 0.5,
             "content": "the propane incident of 2019", "author_name": "Luke"},
        ]


@pytest.mark.asyncio
async def test_retrieval_drops_hits_already_in_the_request():
    """The top hit is reliably the message just sent (it was embedded a
    moment ago, similarity 1.0), and the latest facts are embedded too and
    already printed in full. Each is ~60 tokens of pure duplicate."""
    ctx = await _ctx("what about propane", store=_EchoStore())
    volatile = _split(ctx)[0][1]["text"]
    assert "the propane incident of 2019" in volatile          # new: kept
    assert "Matt likes thing number 3" not in volatile          # in prefix: dropped
    retrieved = volatile.split("Potentially relevant", 1)[1]
    assert "what about propane" not in retrieved                # itself: dropped


@pytest.mark.asyncio
async def test_retrieval_block_is_omitted_when_every_hit_was_a_duplicate():
    class _OnlyEcho(_Store):
        async def semantic_search(self, chat_id, query, k=6):
            return [{"ref_kind": "message", "ref_id": 1, "similarity": 1.0,
                     "content": query, "author_name": "Matt"}]

    ctx = await _ctx("what about propane", store=_OnlyEcho())
    assert "Potentially relevant" not in _split(ctx)[0][1]["text"]
