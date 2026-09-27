"""WB connector settings (pydantic-settings BaseSettings, env_prefix='WB_').

All tunable parameters are configurable via environment variables with a WB_
prefix, e.g. WB_TIMEOUT, WB_WALL_TIMEOUT, WB_MIN_GAP, WB_DEFAULT_DEST,
WB_REGION, WB_MAX_BODY_BYTES, WB_NET_RETRIES, WB_NET_BACKOFF_S, WB_CACHE_TTL, WB_PROXY.

Operational knobs only. The search token is not a setting: it is minted by the
plugin's Chrome profile and cached in ``${CLAUDE_PLUGIN_DATA}`` (``wb_connector.session``).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from wb_connector.regions import WB_REGIONS


class WBSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    timeout: float = Field(
        default=15.0,
        gt=0,
        description="Per-request HTTP timeout in seconds (connect+read combined).",
    )
    wall_timeout: float = Field(
        default=45.0,
        gt=0,
        description="Whole-request wall-clock budget in seconds, bounding retries.",
    )
    min_gap: float = Field(
        default=3.0,
        ge=0,
        description="Polite gap between WB requests in seconds (residential-IP rate limit).",
    )
    default_dest: str = Field(
        default="-1257786",
        description="Default WB region ID when WB_REGION is empty. -1257786 = Moscow, as the site itself sends it.",
    )
    region: str = Field(
        default="",
        description="City name from wb_connector.regions.WB_REGIONS; overrides default_dest when set.",
    )
    max_body_bytes: int = Field(
        default=52428800,
        ge=1024,
        description="Hard cap on response body bytes (CDN compromise / MITM defense).",
    )
    net_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Transport-level retries after the first try (httpx transport errors only).",
    )
    net_backoff_s: float = Field(
        default=0.8,
        ge=0,
        description="Backoff between transport retries in seconds.",
    )
    cache_ttl: float = Field(
        default=120.0,
        ge=0,
        description="Seconds to cache upstream reads. 0 disables caching. An agent asking price, then reviews, then a comparison hits the same SKU repeatedly.",
    )
    proxy: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Optional proxy URL. Empty means honour the standard HTTPS_PROXY/ALL_PROXY variables. "
            "May carry user:pass credentials, so it is a SecretStr: repr/dump show '**********', "
            "and only the outbound fetch ever unwraps it."
        ),
    )
    basket_fallback: str = Field(
        default="basket-28",
        min_length=1,
        pattern=r"^basket-\d+$",
        description="Fallback basket host prefix when SKU vol is not in the lookup table. Must match 'basket-N' pattern.",
    )

    @field_validator("region")
    @classmethod
    def _known_region(cls, value: str) -> str:
        value = value.strip()
        if value and value not in WB_REGIONS:
            raise ValueError(f"unknown WB_REGION {value!r}; known: {', '.join(WB_REGIONS)}")
        return value

    def dest(self) -> str:
        return str(WB_REGIONS[self.region]) if self.region else self.default_dest


@lru_cache(maxsize=1)
def get_settings() -> WBSettings:
    return WBSettings()
