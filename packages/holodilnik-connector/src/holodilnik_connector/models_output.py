"""Pydantic output models for the Holodilnik MCP connector."""

from __future__ import annotations

from mcp_core.models import MetaOutBase, SelfCheckEntryBase, SelfCheckResponseBase
from pydantic import BaseModel, ConfigDict, Field


class MetaOut(MetaOutBase):
    """Holodilnik carries the shared envelope unchanged."""


class HolodilnikSearchItemOut(BaseModel):
    product_id: str | None = Field(default=None, description="The site's article number («артикул»), as on the card.")
    title: str | None = Field(default=None, description="Product title with its kind, as the site shows it.")
    price_rub: float | None = Field(default=None, description="What any buyer pays now; None when absent — never 0.")
    old_price_rub: float | None = Field(default=None, description="Crossed-out price, only when above price_rub.")
    rating: float | None = Field(default=None, description="Average rating, 1..5; None without reviews.")
    review_count: int | None = Field(default=None, description="Number of reviews.")
    availability: str | None = Field(
        default=None, description="Delivery term or «Нет в наличии», as the site words it."
    )
    in_stock: bool | None = Field(
        default=None, description="True with a buy button, False for «Нет в наличии», else None."
    )
    url: str | None = Field(default=None, description="Product URL.")


class HolodilnikSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    query: str = Field(default="", description="Search query text.")
    tier_used: str | None = Field(default=None, description="Fetch tier used (http or cache).")
    city: str | None = Field(default=None, description="City the site priced the page for.")
    total_available: int | None = Field(default=None, description="Matches the site reports.")
    count: int = Field(default=0, description="Number of items returned.")
    items: list[HolodilnikSearchItemOut] = Field(default_factory=list, description="Search result items.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class HolodilnikCardResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    product_id: str = Field(default="", description="The site's article number («артикул»).")
    title: str | None = Field(default=None, description="Product title with its kind.")
    brand: str | None = Field(default=None, description="Brand name.")
    gtin: str | None = Field(default=None, description="Barcode (EAN/GTIN) the page lists.")
    price_rub: float | None = Field(default=None, description="Price any buyer pays now; None when absent — never 0.")
    old_price_rub: float | None = Field(default=None, description="Crossed-out price, only when above price_rub.")
    rating: float | None = Field(default=None, description="Average rating, 1..5; None without reviews.")
    review_count: int | None = Field(default=None, description="Number of reviews.")
    is_available: bool | None = Field(default=None, description="From the page's schema.org offer; None if unstated.")
    city: str | None = Field(default=None, description="City the site priced the page for.")
    url: str = Field(default="", description="Product URL.")
    tier_used: str = Field(default="", description="Fetch tier used: http or cache.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class HolodilnikSelfcheckCheckOut(SelfCheckEntryBase):
    ok: bool | None = Field(default=None, description="Boolean health summary if applicable.")
    baseline: str = Field(default="", description="Baseline identifier used for comparison.")
    reason: str | None = Field(default=None, description="Reason code for non-healthy verdicts.")


class HolodilnikSelfcheckResponse(SelfCheckResponseBase):
    healthy: bool | None = Field(default=None, description="Whether all checks are healthy.")
    checks: dict[str, HolodilnikSelfcheckCheckOut] = Field(default_factory=dict, description="Per-subcheck results.")
