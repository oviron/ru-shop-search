"""Pydantic output models for the VseInstrumenti MCP connector."""

from __future__ import annotations

from mcp_core.models import MetaOutBase, SelfCheckEntryBase, SelfCheckResponseBase
from pydantic import BaseModel, ConfigDict, Field


class MetaOut(MetaOutBase):
    """VseInstrumenti carries the shared envelope unchanged."""


class VseInstrumentiSearchItemOut(BaseModel):
    product_id: str | None = Field(default=None, description="Product id, the number at the end of its URL.")
    title: str | None = Field(default=None, description="Product title as the site shows it.")
    price_rub: float | None = Field(
        default=None, description="What any buyer pays now; None when absent — never 0. B2B prices are not price."
    )
    old_price_rub: float | None = Field(default=None, description="Crossed-out price, only when above price_rub.")
    rating: float | None = Field(default=None, description="Average rating, 1..5; None without reviews.")
    review_count: int | None = Field(default=None, description="Number of reviews.")
    in_stock: bool | None = Field(default=None, description="The site offers it for sale in the city; None if unknown.")
    url: str | None = Field(default=None, description="Product URL; card needs it, a bare id does not open.")


class VseInstrumentiSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    query: str = Field(default="", description="Search query text.")
    tier_used: str | None = Field(default=None, description="Fetch tier used (http or cache).")
    city: str | None = Field(default=None, description="City the site priced the page for (chosen by IP).")
    total_available: int | None = Field(default=None, description="Matches the site reports.")
    count: int = Field(default=0, description="Number of items returned.")
    items: list[VseInstrumentiSearchItemOut] = Field(default_factory=list, description="Search result items.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class VseInstrumentiCardResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    product_id: str = Field(default="", description="Product id.")
    title: str | None = Field(default=None, description="Product title.")
    brand: str | None = Field(default=None, description="Brand name.")
    price_rub: float | None = Field(default=None, description="Price any buyer pays now; None when absent — never 0.")
    old_price_rub: float | None = Field(default=None, description="Crossed-out price, only when above price_rub.")
    rating: float | None = Field(default=None, description="Average rating, 1..5; None without reviews.")
    review_count: int | None = Field(default=None, description="Number of reviews.")
    is_available: bool | None = Field(default=None, description="True on sale, False withdrawn or sold out.")
    city: str | None = Field(default=None, description="City the site priced the page for (chosen by IP).")
    url: str = Field(default="", description="Product URL.")
    tier_used: str = Field(default="", description="Fetch tier used: http or cache.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class VseInstrumentiSelfcheckCheckOut(SelfCheckEntryBase):
    ok: bool | None = Field(default=None, description="Boolean health summary if applicable.")
    baseline: str = Field(default="", description="Baseline identifier used for comparison.")
    reason: str | None = Field(default=None, description="Reason code for non-healthy verdicts.")


class VseInstrumentiSelfcheckResponse(SelfCheckResponseBase):
    healthy: bool | None = Field(default=None, description="Whether all checks are healthy.")
    checks: dict[str, VseInstrumentiSelfcheckCheckOut] = Field(
        default_factory=dict, description="Per-subcheck results."
    )
