"""Pydantic output models for the Citilink MCP connector."""

from __future__ import annotations

from mcp_core.models import MetaOutBase, SelfCheckEntryBase, SelfCheckResponseBase
from pydantic import BaseModel, ConfigDict, Field


class MetaOut(MetaOutBase):
    """Citilink carries the shared envelope unchanged."""


class CitilinkSearchItemOut(BaseModel):
    product_id: str | None = Field(default=None, description="Product slug ending in the numeric id.")
    title: str | None = Field(default=None, description="Product title.")
    price_rub: float | None = Field(default=None, description="Price in rubles; None when absent — never 0.")
    old_price_rub: float | None = Field(default=None, description="Strikethrough price in rubles.")
    rating: float | None = Field(default=None, description="Average rating 0..5; None without reviews.")
    review_count: int | None = Field(default=None, description="Number of reviews.")
    in_stock: bool | None = Field(
        default=None, description="Buyable now; False when the tile offers only an arrival notice."
    )
    url: str | None = Field(default=None, description="Canonical product URL.")


class CitilinkSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    query: str = Field(default="", description="Search query text.")
    tier_used: str | None = Field(default=None, description="Fetch tier used (cdp or cache).")
    city: str | None = Field(default=None, description="City the site priced for.")
    count: int = Field(default=0, description="Number of items returned.")
    items: list[CitilinkSearchItemOut] = Field(default_factory=list, description="Search result items.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class CitilinkCardResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    product_id: str | None = Field(default=None, description="Product slug ending in the numeric id.")
    title: str | None = Field(default=None, description="Product title.")
    brand: str | None = Field(default=None, description="Brand name.")
    price_rub: float | None = Field(default=None, description="Price in rubles; None when absent — never 0.")
    old_price_rub: float | None = Field(default=None, description="Strikethrough price in rubles.")
    is_available: bool | None = Field(default=None, description="Whether the product is sellable now.")
    rating: float | None = Field(default=None, description="Average rating 0..5; None without reviews.")
    review_count: int | None = Field(default=None, description="Number of reviews.")
    city: str | None = Field(default=None, description="City the site priced for.")
    url: str = Field(default="", description="Canonical product URL.")
    tier_used: str = Field(default="", description="Fetch tier used (cdp or cache).")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class CitilinkSelfcheckCheckOut(SelfCheckEntryBase):
    ok: bool | None = Field(default=None, description="Boolean health summary if applicable.")
    baseline: str = Field(default="", description="Baseline identifier used for comparison.")
    reason: str | None = Field(default=None, description="Reason code for non-healthy verdicts.")


class CitilinkSelfcheckResponse(SelfCheckResponseBase):
    healthy: bool | None = Field(default=None, description="Whether all checks are healthy.")
    checks: dict[str, CitilinkSelfcheckCheckOut] = Field(default_factory=dict, description="Per-subcheck results.")
