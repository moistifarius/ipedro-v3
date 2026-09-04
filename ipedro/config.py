"""Application configuration, loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ResponsePolicy = Literal["commands", "mention", "reply", "ambient", "always"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str = Field(..., description="Telegram Bot API token")
    admin_user_ids: str = Field(
        "315660812",
        description="Comma-separated numeric Telegram user IDs allowed to use admin commands.",
    )

    # OpenAI (still used for embeddings, image gen, audio — Claude has no equivalents)
    openai_api_key: str
    openai_organization: str | None = None
    openai_text_model: str = "gpt-4o-mini"
    openai_image_model: str = "gpt-image-1"
    openai_transcription_model: str = "whisper-1"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dim: int = 1536
    # Text-to-speech for the /ether radio-voice broadcast. The synthesized
    # speech is intelligible; the radio FX layer adds the degradation.
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "onyx"

    # Anthropic — used for text completions (chat, summaries, /a, /whatdid, etc.).
    # `text_provider` runtime-selects which provider answers text calls; falls
    # back to OpenAI when the Anthropic key is absent. Admins can flip it
    # live via /ai_provider, persisted in kv_store.
    anthropic_api_key: str | None = None
    claude_text_model: str = "claude-sonnet-5"
    # Cheap models used by `AIClient.cheap_chat / cheap_completion` for
    # classifiers, judges, and short one-liners (~3x cheaper than Sonnet
    # with a separate rate-limit quota). Override via env if you want a
    # different cheap default.
    claude_cheap_model: str = "claude-haiku-4-5"
    openai_cheap_model: str = "gpt-4o-mini"
    text_provider: Literal["claude", "openai"] = "claude"

    # Database
    database_url: str

    # Logging
    log_level: str = "INFO"

    # Memory / context
    context_recent_messages: int = 20
    context_max_tokens: int = 6000
    summary_trigger_messages: int = 80
    summary_keep_recent: int = 20
    semantic_retrieval_k: int = 6

    # Temporal awareness — the bot injects "right now it is …" into the AI
    # context and marks long silences between messages so it can reason
    # about time of day, dates, and how long ago things happened. The
    # timezone is an IANA name (e.g. "America/New_York"); invalid values
    # fall back to UTC at resolution time. Default is San Diego / Pacific
    # since that's where the operator is — override with BOT_TIMEZONE for
    # a different location.
    bot_timezone: str = "America/Los_Angeles"

    # Reddit meme puller (/redditmeme). Reddit blocks generic/duplicate
    # User-Agents; per their API rules a descriptive UA that includes your
    # reddit username reduces the odds of a 403. Defaults to the operator's
    # username; override REDDIT_USER_AGENT in .env to change it.
    reddit_user_agent: str = "python:ipedro:1.0 (by /u/moistifarius)"
    # Reddit's anonymous .json API returns 403 from most servers now, so
    # /redditmeme uses OAuth application-only (read-only) access when these
    # are set. Create a "script" app at https://www.reddit.com/prefs/apps
    # and put the client id + secret here (via REDDIT_CLIENT_ID /
    # REDDIT_CLIENT_SECRET in .env). Empty → fall back to the anonymous
    # endpoint (works only from residential IPs, if at all).
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    # Optional extra meme sources for "meme about X" hunts. Both free:
    # GIPHY_API_KEY from developers.giphy.com, IMGUR_CLIENT_ID from
    # api.imgur.com/oauth2/addclient (choose 'anonymous usage'). Unset →
    # those sources are skipped and Reddit (+ KnowYourMeme query
    # expansion, keyless) carries the hunt.
    giphy_api_key: str = ""
    imgur_client_id: str = ""

    # Per-chat defaults
    default_response_policy_private: ResponsePolicy = "always"
    default_response_policy_group: ResponsePolicy = "mention"
    default_ambient_probability: float = 0.03
    default_persona: str = "dude"

    # Duckhunt
    duckhunt_enabled_by_default: bool = False
    # Spawns are a Poisson process per chat: each tick, every enabled chat
    # independently rolls P(spawn) = 1 - exp(-tick / mean). This produces
    # naturally bursty behavior — sometimes ducks several times an hour,
    # sometimes none for days. Tune `mean_spawn_interval_seconds` to set the
    # average rate. 48 h mean = ~0.5/day per chat — most days have no duck,
    # the occasional day has one or two.
    duckhunt_mean_spawn_interval_seconds: int = 172_800  # ~48h → ~0.5/day
    duckhunt_spawn_tick_seconds: int = 60
    # Hard cap on duck lifetime. Most ducks depart probabilistically well
    # before this via the spawner's leave-roll.
    duckhunt_duck_lifetime_seconds: int = 86_400  # 24h
    # Half-life (in seconds) used by the spawner's probabilistic departure
    # check. With ~4h half-life and the default 24h cap, ~98% of ducks have
    # wandered off by the time they hit the cap.
    duckhunt_duck_half_life_seconds: int = 14_400
    # Min seconds between a user's duck actions — just enough to stop button
    # mashing, not enough to feel like a punishment.
    duckhunt_action_cooldown_seconds: int = 4

    # Share-photo idle behavior. Same Poisson shape as duckhunt: each enabled
    # chat rolls per tick. Default mean is 1 day because image generation is
    # not cheap; tune via env if you want more frequent (or fewer) snapshots.
    share_photo_enabled_by_default: bool = False
    share_photo_mean_interval_seconds: int = 86_400  # ~1 photo per chat per day
    share_photo_tick_seconds: int = 300

    @field_validator("admin_user_ids")
    @classmethod
    def _strip_whitespace(cls, v: str) -> str:
        return v.strip()

    @property
    def tzinfo(self):
        """Resolve bot_timezone to a tzinfo, falling back to UTC if the
        configured name isn't a valid IANA zone (or zoneinfo's database
        isn't installed)."""
        from datetime import timezone
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            return ZoneInfo(self.bot_timezone)
        except (ZoneInfoNotFoundError, ValueError, ModuleNotFoundError):
            return timezone.utc

    @property
    def admin_ids(self) -> frozenset[int]:
        """Parsed admin user IDs as a frozen set of ints."""
        ids: set[int] = set()
        for part in self.admin_user_ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                continue
        # 315660812 is always considered an admin per project requirements.
        ids.add(315660812)
        return frozenset(ids)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
