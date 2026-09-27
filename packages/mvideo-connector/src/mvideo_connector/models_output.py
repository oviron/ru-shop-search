"""Pydantic output models for the M.Video MCP connector."""

from __future__ import annotations

from mcp_core.models import MetaOutBase, SelfCheckEntryBase, SelfCheckResponseBase
from pydantic import BaseModel, ConfigDict, Field


class MetaOut(MetaOutBase):
    """M.Video carries the shared envelope unchanged."""


class MVideoSearchItemOut(BaseModel):
    product_id: str | None = Field(default=None, description="M.Video product id (e.g. 400678996).")
    title: str | None = Field(default=None, description="Product title as the site shows it.")
    brand: str | None = Field(default=None, description="Brand name.")
    price_rub: float | None = Field(
        default=None, description="What any buyer pays now; None when absent — never 0. Bonus roubles are not price."
    )
    old_price_rub: float | None = Field(default=None, description="Crossed-out price, only when above price_rub.")
    rating: float | None = Field(default=None, description="Average rating, 1..5; None without reviews.")
    review_count: int | None = Field(default=None, description="Number of reviews.")
    in_stock: bool | None = Field(
        default=None, description="True when available, False when sold out, None for pre-order or unknown."
    )
    marketplace: bool = Field(default=False, description="Sold by a marketplace partner, not M.Video (title «(МП)»).")
    url: str | None = Field(default=None, description="Canonical product URL.")


class MVideoSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    query: str = Field(default="", description="Search query text.")
    tier_used: str | None = Field(default=None, description="Fetch tier used (http or cache).")
    city: str | None = Field(default=None, description="City the prices were requested for.")
    total_available: int | None = Field(default=None, description="Matches the site reports.")
    count: int = Field(default=0, description="Number of items returned.")
    items: list[MVideoSearchItemOut] = Field(default_factory=list, description="Search result items.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class MVideoCardResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    product_id: str = Field(default="", description="M.Video product id.")
    title: str | None = Field(default=None, description="Product title.")
    brand: str | None = Field(default=None, description="Brand name.")
    price_rub: float | None = Field(default=None, description="Price the page shows; None when absent — never 0.")
    old_price_rub: float | None = Field(default=None, description="Crossed-out price, only when above price_rub.")
    rating: float | None = Field(default=None, description="Average rating, 1..5; None without reviews.")
    review_count: int | None = Field(default=None, description="Number of reviews.")
    is_available: bool | None = Field(
        default=None, description="True in stock, False sold out, None for pre-order or unknown."
    )
    marketplace: bool = Field(default=False, description="Sold by a marketplace partner, not M.Video (title «(МП)»).")
    url: str = Field(default="", description="Canonical product URL.")
    tier_used: str = Field(default="", description="Fetch tier used: cdp or cache.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class MVideoSelfcheckCheckOut(SelfCheckEntryBase):
    ok: bool | None = Field(default=None, description="Boolean health summary if applicable.")
    baseline: str = Field(default="", description="Baseline identifier used for comparison.")
    reason: str | None = Field(default=None, description="Reason code for non-healthy verdicts.")


class MVideoSelfcheckResponse(SelfCheckResponseBase):
    healthy: bool | None = Field(default=None, description="Whether all checks are healthy.")
    checks: dict[str, MVideoSelfcheckCheckOut] = Field(default_factory=dict, description="Per-subcheck results.")
