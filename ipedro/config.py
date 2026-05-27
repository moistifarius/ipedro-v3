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

    # OpenAI
    openai_api_key: str
    openai_organization: str | None = None
    openai_text_model: str = "gpt-4o-mini"
    openai_image_model: str = "gpt-image-1"
    openai_transcription_model: str = "whisper-1"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dim: int = 1536

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
    default_persona: str = "pedro"

    # Duckhunt
    duckhunt_enabled_by_default: bool = False
    duckhunt_min_spawn_seconds: int = 900
    duckhunt_max_spawn_seconds: int = 5400
    # Hard cap on duck lifetime. Most ducks depart probabilistically well
    # before this via the spawner's leave-roll.
    duckhunt_duck_lifetime_seconds: int = 86_400  # 24h
    # Half-life (in seconds) used by the spawner's probabilistic departure
    # check. With ~4h half-life and the default 24h cap, ~98% of ducks have
    # wandered off by the time they hit the cap.
    duckhunt_duck_half_life_seconds: int = 14_400
    duckhunt_action_cooldown_seconds: int = 15

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
