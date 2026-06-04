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
    claude_text_model: str = "claude-sonnet-4-6"
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
    # average rate.
    duckhunt_mean_spawn_interval_seconds: int = 14_400  # ~4h average
    duckhunt_spawn_tick_seconds: int = 60
    # Hard cap on duck lifetime. Most ducks depart probabilistically well
    # before this via the spawner's leave-roll.
    duckhunt_duck_lifetime_seconds: int = 86_400  # 24h
    # Half-life (in seconds) used by the spawner's probabilistic departure
    # check. With ~4h half-life and the default 24h cap, ~98% of ducks have
    # wandered off by the time they hit the cap.
    duckhunt_duck_half_life_seconds: int = 14_400
    duckhunt_action_cooldown_seconds: int = 15

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
