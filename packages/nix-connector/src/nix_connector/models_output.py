"""Pydantic output models for the Nix MCP connector."""

from __future__ import annotations

from mcp_core.models import MetaOutBase, SelfCheckEntryBase, SelfCheckResponseBase
from pydantic import BaseModel, ConfigDict, Field


class MetaOut(MetaOutBase):
    """Nix carries the shared envelope unchanged."""


class NixSearchItemOut(BaseModel):
    product_id: str | None = Field(default=None, description="Product id, the number at the end of its URL.")
    title: str | None = Field(default=None, description="Product title as the site shows it.")
    price_rub: float | None = Field(
        default=None,
        description="The site's price (the «до» column); None when absent — never 0. "
        "СУПЕРЦЕНА (prepay, store pickup) is not the price.",
    )
    availability: str | None = Field(
        default=None, description="The «Наличие» column as shown: ЕСТЬ, ЗАВТРА, ПОСЛЕЗАВТРА or a dd.mm date."
    )
    in_stock: bool | None = Field(default=None, description="True when the column names a term; None if unknown.")
    url: str | None = Field(default=None, description="Product URL; nix_card needs it.")


class NixSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    query: str = Field(default="", description="Search query text.")
    tier_used: str | None = Field(default=None, description="Fetch tier used (cdp or cache).")
    city: str | None = Field(default=None, description="City the site priced the page for.")
    total_available: int | None = Field(default=None, description="Matches the site reports.")
    count: int = Field(default=0, description="Number of items returned.")
    items: list[NixSearchItemOut] = Field(default_factory=list, description="Search result items.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class NixCardResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    product_id: str = Field(default="", description="Product id.")
    title: str | None = Field(default=None, description="Product title.")
    brand: str | None = Field(default=None, description="Brand name.")
    part_number: str | None = Field(default=None, description="Manufacturer part number (schema.org sku).")
    price_rub: float | None = Field(default=None, description="The site's price; None when absent — never 0.")
    is_available: bool | None = Field(default=None, description="True on sale, False sold out or withdrawn.")
    city: str | None = Field(default=None, description="City the site priced the page for.")
    url: str = Field(default="", description="Product URL.")
    tier_used: str = Field(default="", description="Fetch tier used: http or cache.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class NixSelfcheckCheckOut(SelfCheckEntryBase):
    ok: bool | None = Field(default=None, description="Boolean health summary if applicable.")
    baseline: str = Field(default="", description="Baseline identifier used for comparison.")
    reason: str | None = Field(default=None, description="Reason code for non-healthy verdicts.")


class NixSelfcheckResponse(SelfCheckResponseBase):
    healthy: bool | None = Field(default=None, description="Whether all checks are healthy.")
    checks: dict[str, NixSelfcheckCheckOut] = Field(default_factory=dict, description="Per-subcheck results.")
