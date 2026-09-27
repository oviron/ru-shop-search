"""Pydantic output models for the Pepper MCP connector."""

from __future__ import annotations

from mcp_core.models import MetaOutBase, SelfCheckEntryBase, SelfCheckResponseBase
from pydantic import BaseModel, ConfigDict, Field

_DEAL_PRICE = (
    "Price the deal post names for the merchant, in roubles; None when absent — never 0. "
    "A user reported it when posting: it may be gone, and conditions in the title (subscription, "
    "wallet, card, import) may apply. Not a shop price."
)


class MetaOut(MetaOutBase):
    """Pepper carries the shared envelope unchanged."""


class PepperDealOut(BaseModel):
    deal_id: str | None = Field(default=None, description="Deal id, the number at the end of its URL.")
    title: str | None = Field(default=None, description="Deal title as posted, conditions included.")
    merchant: str | None = Field(default=None, description="Shop the deal points to (Wildberries, OZON, …).")
    deal_price_rub: float | None = Field(default=None, description=_DEAL_PRICE)
    old_price_rub: float | None = Field(default=None, description="Crossed-out price in the post, only when above.")
    posted: str | None = Field(default=None, description="When it was posted, as the site words it («10 дней назад»).")
    expired: bool = Field(default=False, description="The site marks the deal «Истекло».")
    temperature: int | None = Field(default=None, description="Community vote score in degrees; can be negative.")
    url: str | None = Field(default=None, description="Deal page on pepper.ru.")


class PepperSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    query: str = Field(default="", description="Search query text.")
    tier_used: str | None = Field(default=None, description="Fetch tier used (cdp or cache).")
    total_available: int | None = Field(default=None, description="Deals the site reports for the query.")
    count: int = Field(default=0, description="Number of deals returned.")
    items: list[PepperDealOut] = Field(default_factory=list, description="Deals, in the site's order.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class PepperCardResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    deal_id: str = Field(default="", description="Deal id.")
    title: str | None = Field(default=None, description="Deal title as posted, conditions included.")
    merchant: str | None = Field(default=None, description="Shop the deal points to.")
    deal_price_rub: float | None = Field(default=None, description=_DEAL_PRICE)
    published_at: str | None = Field(default=None, description="When the deal was posted, ISO 8601.")
    valid_until: str | None = Field(default=None, description="End date the poster gave, as the site states it.")
    expired: bool = Field(default=False, description="The site says the deal has expired.")
    temperature: int | None = Field(default=None, description="Community vote score in degrees; can be negative.")
    url: str = Field(default="", description="Deal page on pepper.ru.")
    tier_used: str = Field(default="", description="Fetch tier used: cdp or cache.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class PepperSelfcheckCheckOut(SelfCheckEntryBase):
    ok: bool | None = Field(default=None, description="Boolean health summary if applicable.")
    baseline: str = Field(default="", description="Baseline identifier used for comparison.")
    reason: str | None = Field(default=None, description="Reason code for non-healthy verdicts.")


class PepperSelfcheckResponse(SelfCheckResponseBase):
    healthy: bool | None = Field(default=None, description="Whether all checks are healthy.")
    checks: dict[str, PepperSelfcheckCheckOut] = Field(default_factory=dict, description="Per-subcheck results.")
