"""Pydantic output models for the DNS-Shop MCP connector."""

from __future__ import annotations

from mcp_core.models import MetaOutBase, SelfCheckEntryBase, SelfCheckResponseBase
from pydantic import BaseModel, ConfigDict, Field


class MetaOut(MetaOutBase):
    """DNS carries the shared envelope unchanged."""


class DnsSearchItemOut(BaseModel):
    product_id: str | None = Field(default=None, description="DNS product id/slug tail.")
    title: str | None = Field(default=None, description="Product title.")
    price_rub: float | None = Field(default=None, description="Price in rubles; None when absent — never 0.")
    old_price_rub: float | None = Field(default=None, description="Strikethrough price in rubles.")
    availability: str | None = Field(
        default=None, description="Stock label on the tile, e.g. «В наличии», «При заказе»."
    )
    in_stock: bool | None = Field(
        default=None, description="True for «В наличии», False for an explicit absence, None otherwise."
    )
    url: str | None = Field(default=None, description="Canonical product URL.")


class DnsSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    query: str = Field(default="", description="Search query text.")
    tier_used: str | None = Field(default=None, description="Fetch tier used (cdp).")
    city: str | None = Field(default=None, description="City DNS priced the page for (chosen by the site from the IP).")
    count: int = Field(default=0, description="Number of items returned.")
    items: list[DnsSearchItemOut] = Field(default_factory=list, description="Search result items.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class DnsCardResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    product_id: str | None = Field(default=None, description="DNS product id/slug tail.")
    title: str | None = Field(default=None, description="Product title.")
    price_rub: float | None = Field(default=None, description="Price in rubles; None when absent — never 0.")
    old_price_rub: float | None = Field(default=None, description="Strikethrough price in rubles.")
    is_available: bool | None = Field(default=None, description="Whether the product is sellable now.")
    city: str | None = Field(default=None, description="City DNS priced the page for (chosen by the site from the IP).")
    url: str = Field(default="", description="Canonical product URL.")
    tier_used: str = Field(default="", description="Fetch tier used (cdp).")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class DnsPricePoint(BaseModel):
    week: str = Field(description="Start of the week, ISO date; empty when the label cannot be read.")
    label: str = Field(description="The week as DNS labels it, e.g. 13.04-19.04.")
    price_rub: float = Field(description="Average selling price DNS shows for that week, in roubles.")


class DnsPriceHistoryResponse(BaseModel):
    product_id: str = Field(description="DNS product id from the URL.")
    title: str | None = Field(default=None, description="Product title from the page.")
    points: list[DnsPricePoint] = Field(default_factory=list, description="Weekly points, oldest first.")
    price_range: str | None = Field(default=None, description="The range line DNS shows above the chart.")
    url: str = Field(default="", description="Canonical product URL.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class DnsSelfcheckCheckOut(SelfCheckEntryBase):
    ok: bool | None = Field(default=None, description="Boolean health summary if applicable.")
    baseline: str = Field(default="", description="Baseline identifier used for comparison.")
    reason: str | None = Field(default=None, description="Reason code for non-healthy verdicts.")


class DnsSelfcheckResponse(SelfCheckResponseBase):
    healthy: bool | None = Field(default=None, description="Whether all checks are healthy.")
    checks: dict[str, DnsSelfcheckCheckOut] = Field(default_factory=dict, description="Per-subcheck results.")
