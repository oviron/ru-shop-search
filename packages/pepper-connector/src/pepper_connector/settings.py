"""Pepper connector runtime settings (env-driven via PEPPER_ prefix)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024


class PepperSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PEPPER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    min_gap: float = Field(default=3.0, ge=0)
    cache_ttl: float = Field(default=300.0, ge=0, description="Seconds to cache page reads. 0 disables caching.")
    max_body_bytes: int = Field(default=_DEFAULT_MAX_BODY_BYTES, gt=0)
    selfcheck_query: str = Field(default="rtx 5070", min_length=1)


@lru_cache(maxsize=1)
def get_settings() -> PepperSettings:
    return PepperSettings()
