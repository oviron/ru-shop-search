"""Pydantic output models for the Lamoda MCP connector."""

from __future__ import annotations

from mcp_core.models import MetaOutBase, SelfCheckEntryBase, SelfCheckResponseBase
from pydantic import BaseModel, ConfigDict, Field


class MetaOut(MetaOutBase):
    """Lamoda carries the shared envelope unchanged."""


class LamodaSearchItemOut(BaseModel):
    sku: str | None = Field(default=None, description="Lamoda SKU (e.g. MP002XM1RMM3).")
    title: str | None = Field(default=None, description="Product title.")
    brand: str | None = Field(default=None, description="Brand name.")
    price_rub: float | None = Field(
        default=None, description="What any buyer pays now, site promo included; None when absent — never 0."
    )
    old_price_rub: float | None = Field(default=None, description="Price before every discount.")
    promo_until: str | None = Field(default=None, description="When the site promo inside price_rub ends, ISO time.")
    rating: float | None = Field(default=None, description="Average rating, 1..5.")
    review_count: int | None = Field(default=None, description="Number of reviews.")
    in_stock: bool | None = Field(default=None, description="Whether some size can be ordered now.")
    url: str | None = Field(default=None, description="Canonical product URL.")


class LamodaSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    query: str = Field(default="", description="Search query text.")
    tier_used: str | None = Field(default=None, description="Fetch tier used (http).")
    total_available: int | None = Field(default=None, description="Matches the site reports.")
    count: int = Field(default=0, description="Number of items returned.")
    items: list[LamodaSearchItemOut] = Field(default_factory=list, description="Search result items.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class LamodaSizeOut(BaseModel):
    size: str | None = Field(default=None, description="Size label.")
    is_available: bool | None = Field(default=None, description="Whether the size is in stock.")


class LamodaCardResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    sku: str | None = Field(default=None, description="Lamoda SKU.")
    title: str | None = Field(default=None, description="Product title.")
    brand: str | None = Field(default=None, description="Brand name.")
    price_rub: float | None = Field(
        default=None, description="What any buyer pays now, site promo included; None when absent — never 0."
    )
    old_price_rub: float | None = Field(default=None, description="Price before every discount.")
    promo_until: str | None = Field(default=None, description="When the site promo inside price_rub ends, ISO time.")
    rating: float | None = Field(default=None, description="Average rating, 1..5.")
    review_count: int | None = Field(default=None, description="Number of reviews.")
    seller: str | None = Field(default=None, description="Seller name; Lamoda for its own stock.")
    is_available: bool | None = Field(default=None, description="Whether the product is in stock now.")
    sizes: list[LamodaSizeOut] = Field(default_factory=list, description="Per-size availability.")
    url: str = Field(default="", description="Canonical product URL.")
    tier_used: str = Field(default="", description="Fetch tier used: http.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class LamodaSelfcheckCheckOut(SelfCheckEntryBase):
    ok: bool | None = Field(default=None, description="Boolean health summary if applicable.")
    baseline: str = Field(default="", description="Baseline identifier used for comparison.")
    reason: str | None = Field(default=None, description="Reason code for non-healthy verdicts.")


class LamodaSelfcheckResponse(SelfCheckResponseBase):
    healthy: bool | None = Field(default=None, description="Whether all checks are healthy.")
    checks: dict[str, LamodaSelfcheckCheckOut] = Field(default_factory=dict, description="Per-subcheck results.")
