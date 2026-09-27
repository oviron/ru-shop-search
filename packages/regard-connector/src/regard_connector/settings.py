"""Regard connector runtime settings (env-driven via REGARD_ prefix)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_MAX_BODY_BYTES = 50 * 1024 * 1024


class RegardSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REGARD_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    timeout: float = Field(default=30.0, gt=0)
    min_gap: float = Field(default=3.0, ge=0)
    cache_ttl: float = Field(default=120.0, ge=0, description="Seconds to cache upstream reads. 0 disables caching.")
    max_body_bytes: int = Field(default=_DEFAULT_MAX_BODY_BYTES, gt=0)
    selfcheck_query: str = Field(default="rtx 5070", min_length=1)
    proxy: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Optional proxy URL. Empty honours REGARD_PROXY, then HTTPS_PROXY/ALL_PROXY. "
            "May carry user:pass credentials, so it is a SecretStr: repr/dump show '**********', "
            "and only the outbound fetch ever unwraps it."
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> RegardSettings:
    return RegardSettings()
