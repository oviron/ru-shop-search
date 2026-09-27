"""Pydantic output models for the Regard MCP connector."""

from __future__ import annotations

from mcp_core.models import MetaOutBase, SelfCheckEntryBase, SelfCheckResponseBase
from pydantic import BaseModel, ConfigDict, Field


class MetaOut(MetaOutBase):
    """Regard carries the shared envelope unchanged."""


class RegardSearchItemOut(BaseModel):
    product_id: str | None = Field(default=None, description="Product id, the number in its URL.")
    title: str | None = Field(default=None, description="Product title with its kind, as the site shows it.")
    brand: str | None = Field(default=None, description="Brand name.")
    part_number: str | None = Field(default=None, description="Manufacturer part number the site lists.")
    price_rub: float | None = Field(default=None, description="What any buyer pays now; None when absent — never 0.")
    old_price_rub: float | None = Field(default=None, description="Crossed-out price, only when above price_rub.")
    rating: float | None = Field(default=None, description="Average rating, 1..5; None without reviews.")
    review_count: int | None = Field(default=None, description="Number of reviews.")
    in_stock: bool | None = Field(
        default=None, description="True when the site gives a delivery term and it is not a pre-order; else None."
    )
    url: str | None = Field(default=None, description="Product URL.")


class RegardSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    query: str = Field(default="", description="Search query text.")
    tier_used: str | None = Field(default=None, description="Fetch tier used (http or cache).")
    city: str | None = Field(default=None, description="City the site priced the page for.")
    total_available: int | None = Field(default=None, description="Matches the site reports.")
    count: int = Field(default=0, description="Number of items returned.")
    items: list[RegardSearchItemOut] = Field(default_factory=list, description="Search result items.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class RegardCardResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    product_id: str = Field(default="", description="Product id.")
    title: str | None = Field(default=None, description="Product title with its kind.")
    brand: str | None = Field(default=None, description="Brand name.")
    part_number: str | None = Field(default=None, description="Manufacturer part number the site lists.")
    price_rub: float | None = Field(default=None, description="Price any buyer pays now; None when absent — never 0.")
    old_price_rub: float | None = Field(default=None, description="Crossed-out price, only when above price_rub.")
    rating: float | None = Field(default=None, description="Average rating, 1..5; None without reviews.")
    review_count: int | None = Field(default=None, description="Number of reviews.")
    is_available: bool | None = Field(default=None, description="From the page's schema.org offer; None if unstated.")
    city: str | None = Field(default=None, description="City the site priced the page for.")
    url: str = Field(default="", description="Product URL.")
    tier_used: str = Field(default="", description="Fetch tier used: http or cache.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class RegardSelfcheckCheckOut(SelfCheckEntryBase):
    ok: bool | None = Field(default=None, description="Boolean health summary if applicable.")
    baseline: str = Field(default="", description="Baseline identifier used for comparison.")
    reason: str | None = Field(default=None, description="Reason code for non-healthy verdicts.")


class RegardSelfcheckResponse(SelfCheckResponseBase):
    healthy: bool | None = Field(default=None, description="Whether all checks are healthy.")
    checks: dict[str, RegardSelfcheckCheckOut] = Field(default_factory=dict, description="Per-subcheck results.")
