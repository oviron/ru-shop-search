"""Pydantic output models for the Avito MCP connector.

Every tool returns a typed Pydantic model instead of a raw dict. The ``meta``
field is populated from the ``_meta`` key that ``resilience.attach_meta``
writes, and serializes back to ``meta`` on the wire.
"""

from __future__ import annotations

from mcp_core.models import MetaOutBase, SelfCheckEntryBase, SelfCheckResponseBase
from pydantic import BaseModel, ConfigDict, Field


class MetaOut(MetaOutBase):
    """Avito carries the shared envelope unchanged."""


class AvitoSearchItemOut(BaseModel):
    item_id: int | None = Field(default=None, description="Avito item id (the digits in the listing URL).")
    title: str | None = Field(default=None, description="Listing title.")
    price_rub: float | None = Field(default=None, description="Price in rubles; None when the listing has no price.")
    url: str | None = Field(default=None, description="Canonical avito.ru listing URL.")
    location: str | None = Field(default=None, description="Seller location string.")
    category_id: int | None = Field(default=None, description="Avito category id of the listing.")
    seller_rating: float | None = Field(default=None, description="Seller rating 0-5 shown on the listing.")
    seller_reviews: int | None = Field(default=None, description="Seller review count shown on the listing.")
    is_company: bool | None = Field(default=None, description="Whether the seller is a company profile.")
    posted_at: str | None = Field(default=None, description="Publication time as reported by Avito.")
    images: int = Field(default=0, description="Number of images attached to the listing.")


class AvitoSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    query: str = Field(default="", description="Search query text.")
    page: int = Field(default=1, description="Result page number.")
    location_id: str = Field(default="", description="Avito location id the search ran against.")
    tier_used: str | None = Field(default=None, description="cdp (the plugin's tab) or cache.")
    count: int = Field(default=0, description="Number of items returned on this page.")
    total_count: int | None = Field(default=None, description="Total matches Avito reports for the query.")
    items: list[AvitoSearchItemOut] = Field(default_factory=list, description="Search result items.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class AvitoCardSellerOut(BaseModel):
    is_company: bool | None = Field(default=None, description="Company (True) or private person (False).")
    rating_score: float | None = Field(default=None, description="Seller rating 0-5.")
    rating_count: int | None = Field(default=None, description="Seller review count.")
    since_year: int | None = Field(default=None, description="Year the seller joined Avito.")


class AvitoCardResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(default="success", description="Response status: success or error.")
    item_id: str = Field(description="Avito item id.")
    title: str | None = Field(default=None, description="Listing title.")
    price_rub: float | None = Field(default=None, description="Price in rubles; None when the listing has no price.")
    description: str | None = Field(default=None, description="Listing description text.")
    params: list[str] = Field(default_factory=list, description="Listing parameters as «name: value».")
    posted: str | None = Field(default=None, description="Publication time as the page shows it.")
    views: int | None = Field(default=None, description="Total view count.")
    photos: int = Field(default=0, description="Number of photos.")
    delivery: bool | None = Field(default=None, description="Whether the page offers Avito delivery.")
    seller: AvitoCardSellerOut = Field(default_factory=AvitoCardSellerOut, description="Seller reputation, no name.")
    url: str = Field(description="Canonical avito.ru listing URL.")
    tier_used: str = Field(default="", description="cdp or cache.")
    meta: MetaOut = Field(default_factory=MetaOut, alias="_meta", description="Validation metadata.")


class AvitoSelfcheckCheckOut(SelfCheckEntryBase):
    """Avito sub-check entry: adds the baseline-comparison fields."""

    ok: bool | None = Field(default=None, description="Boolean health summary if applicable.")
    baseline: str = Field(default="", description="Baseline identifier used for comparison.")
    reason: str | None = Field(default=None, description="Reason code for non-healthy verdicts.")


class AvitoSelfcheckResponse(SelfCheckResponseBase):
    healthy: bool | None = Field(default=None, description="Whether all checks are healthy.")
    checks: dict[str, AvitoSelfcheckCheckOut] = Field(default_factory=dict, description="Per-subcheck results.")


class DefectOut(BaseModel):
    severity: str = Field(description="critical, serious or minor.")
    label: str = Field(description="Defect family, e.g. parts, dead, locked, broken_screen, wear.")
    phrase: str = Field(description="The words in the listing that matched.")
    penalty: int = Field(description="Points this defect takes off the score.")


class EvaluationPart(BaseModel):
    factor: str = Field(description="price, seller, transparency or delivery.")
    points: int = Field(description="Points earned.")
    max_points: int = Field(description="Points available for this factor.")
    note: str = Field(default="", description="Why these points.")


class AvitoEvaluation(BaseModel):
    verdict: str = Field(description="good_deal, fair, caution, avoid or insufficient_sample.")
    score: int | None = Field(default=None, description="0-100; None when there is no price reference.")
    title: str = Field(default="", description="Listing title as evaluated.")
    price_rub: float | None = Field(default=None, description="Listing price as evaluated.")
    url: str | None = Field(default=None, description="Listing URL when one was given.")
    breakdown: list[EvaluationPart] = Field(default_factory=list, description="Points per factor.")
    defects: list[DefectOut] = Field(default_factory=list, description="Defects found in title, description, params.")
    defect_penalty: int = Field(default=0, description="Total taken off for defects, capped.")
    flags: list[str] = Field(default_factory=list, description="Risk flags, e.g. suspiciously_cheap, new_seller.")
    avito_median_rub: float | None = Field(default=None, description="Median of comparable Avito listings.")
    avito_sample: int = Field(default=0, description="How many comparable Avito listings the median uses.")
    min_avito_sample: int = Field(default=0, description="Smallest sample that gives a verdict.")
    price_to_avito_median: float | None = Field(default=None, description="Listing price / Avito median.")
    retail_median_rub: float | None = Field(default=None, description="Median new price in retail shops.")
    retail_sample: int = Field(default=0, description="How many retail prices the median uses.")
    min_retail_sample: int = Field(default=0, description="Smallest retail sample that is used.")
    price_to_retail_median: float | None = Field(default=None, description="Listing price / retail median.")
    notes: list[str] = Field(default_factory=list, description="What was missing or not checked.")
