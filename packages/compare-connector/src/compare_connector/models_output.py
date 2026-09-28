"""Typed responses for cross-marketplace comparison.

The schema is built around one honesty requirement: a comparison that silently
omits a marketplace is worse than one that says it failed. Hence per-source
outcomes alongside the merged offer list, and a ``complete`` flag the caller can
check before drawing conclusions.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from compare_connector.identity import OfferEvidence, ProductIdentity


class DealInfo(BaseModel):
    """A community deal post (Pepper): a price someone saw at a shop, not a shop offer."""

    price_rub: float | None = Field(default=None, description="Price the poster saw at the merchant; never ranked.")
    old_price_rub: float | None = Field(default=None, description="Crossed-out price in the post, when shown.")
    merchant: str = Field(default="", description="Shop the deal points to.")
    posted: str | None = Field(default=None, description="Post age as the site shows it («10 дней назад»).")
    expired: bool = Field(default=False, description="The site marks the deal as ended.")
    temperature: int | None = Field(default=None, description="Community vote score; negative means disputed.")


class MarketOffer(BaseModel):
    """One offer, normalised across marketplaces so prices are comparable."""

    source: str = Field(default="", description="Marketplace the offer came from (wildberries, yandex_market, ozon).")
    product_id: str = Field(default="", description="Marketplace-native product id, for a follow-up card lookup.")
    variant_id: str = Field(default="", description="Sellable variant/SKU id when the source exposes one separately.")
    title: str = Field(default="", description="Product title as the marketplace names it.")
    brand: str = Field(default="", description="Brand name, when reported.")
    seller: str = Field(default="", description="Seller/shop name, when reported.")
    price_rub: float | None = Field(
        default=None,
        description=(
            "Price in roubles — the only basis for ranking. None when absent, never 0, "
            "and always None for a marketplace that quotes in another currency. "
            "price_condition names what the buyer must do to get it."
        ),
    )
    price_condition: str = Field(
        default="",
        description=(
            "Empty when anyone pays price_rub. ozon_card: free Ozon Card; superprice: Nix prepay and store "
            "pickup; coupon: an AliExpress coupon to collect."
        ),
    )
    price_regular_rub: float | None = Field(
        default=None, description="Price without price_condition, when the shop shows it; None otherwise."
    )
    currency: str = Field(
        default="rub",
        description="Currency of price_native, lowercase ISO code. Only 'rub' offers enter the ranking.",
    )
    price_native: float | None = Field(
        default=None,
        description=("Price as the marketplace quotes it, in `currency`. Equals price_rub for rouble offers."),
    )
    price_with_subscription_rub: float | None = Field(
        default=None,
        description=(
            "Lower price needing a paid subscription (Yandex Plus). "
            "Excluded from ranking, since an anonymous buyer does not get it."
        ),
    )
    rating: float | None = Field(default=None, description="Average rating, 1..5, when reported.")
    rating_count: int | None = Field(default=None, description="Number of ratings or reviews behind that average.")
    in_stock: bool | None = Field(default=None, description="Stock status, when the marketplace reports it.")
    url: str = Field(default="", description="Direct product URL.")
    identity: ProductIdentity = Field(
        default_factory=ProductIdentity,
        description="Manufacturer and variant identity evidence; empty means identity is unknown.",
    )
    evidence: OfferEvidence | None = Field(
        default=None,
        description="Optional provenance for the native offer and its identity evidence.",
    )
    deal: DealInfo | None = Field(
        default=None,
        description=(
            "Set for a deal post (Pepper): price_rub stays None and the post's price lives here, "
            "to be checked at the merchant before quoting."
        ),
    )

    @model_validator(mode="after")
    def _mirror_rouble_price_into_native(self) -> MarketOffer:
        """For rouble sources the native price is the rouble price.

        Filling this here rather than in nine adapters means price_native is
        always the number the marketplace actually showed, whatever the source,
        so a caller can read one field without special-casing a source.
        """
        if self.price_native is None and self.currency == "rub":
            self.price_native = self.price_rub
        return self

    @model_validator(mode="after")
    def _deal_has_no_ranking_price(self) -> MarketOffer:
        if self.deal is not None and self.price_rub is not None:
            raise ValueError("a deal post has no ranking price: keep price_rub None and the price in deal")
        return self


class SourceOutcome(BaseModel):
    """What happened when one marketplace was queried.

    Reported for every source, successful or not — that is what lets a caller
    tell "cheapest overall" from "cheapest among the two that answered".
    """

    source: str = Field(default="", description="Marketplace name.")
    status: str = Field(
        default="",
        description=(
            "ok, blocked (the shop refused: 401/403/418/429, captcha or anti-bot page, or it is stopped "
            "after such a refusal), timeout, error (5xx, crashed tab, parse failure) or not_installed."
        ),
    )
    detail: str = Field(default="", description="Human-readable outcome detail; the error text when it failed.")
    warnings: list[str] = Field(
        default_factory=list, description="Native diagnostics; successful access can still return degraded data."
    )
    offers_returned: int = Field(default=0, description="How many offers this marketplace contributed.")
    elapsed_ms: int = Field(default=0, description="Round-trip time for this marketplace, in milliseconds.")
    error_code: str | None = Field(default=None, description="Structured connector error code, if known.")
    retryable: bool = Field(default=False, description="A later retry may succeed; see retry_after_s.")
    retry_after_s: int | None = Field(
        default=None, description="Seconds until a stopped source accepts requests again, when known."
    )
    challenge_type: str | None = Field(
        default=None, description="captcha, login, login_or_captcha, redirect or antibot_page when known."
    )


class CompareResponse(BaseModel):
    """A ranked cross-marketplace price comparison with per-source provenance."""

    query: str = Field(default="", description="The query that was priced.")
    sources_queried: list[str] = Field(default_factory=list, description="Marketplaces that were attempted.")
    sources_ok: list[str] = Field(default_factory=list, description="Marketplaces that answered successfully.")
    complete: bool = Field(
        default=False,
        description="True only when every queried marketplace answered. False means the ranking is partial.",
    )
    total_offers: int = Field(default=0, description="Total offers across all marketplaces.")
    cheapest: MarketOffer | None = Field(
        default=None,
        description="Lowest everyday price found. None when no marketplace returned a price.",
    )
    cheapest_comparable: MarketOffer | None = Field(
        default=None,
        description=(
            "Lowest everyday price among offers whose titles do not identify an accessory or a used/display condition. "
            "Use this for a like-for-like recommendation; cheapest retains the raw lowest listing."
        ),
    )
    price_spread_rub: float | None = Field(
        default=None,
        description="Difference between the highest and lowest everyday price — how much the choice is worth.",
    )
    offers: list[MarketOffer] = Field(
        default_factory=list,
        description="All offers, cheapest first. Offers without a price are kept at the end.",
    )
    source_outcomes: list[SourceOutcome] = Field(
        default_factory=list,
        description="Per-marketplace outcome, including failures — read this before trusting the ranking.",
    )
    warnings: list[str] = Field(default_factory=list, description="Connector-level warnings (partial data, no prices).")
    server_version: str = Field(default="", description="Connector version.")


class ModelGroup(BaseModel):
    """Offers from different shops that describe one model, with how sure the match is."""

    confidence: str = Field(
        description=(
            "exact: the same valid GTIN, or the same brand and manufacturer part number; "
            "likely: the same model code in the titles or a typed part number; "
            "possible: the same Latin words and numbers in the titles, colours and wording aside."
        )
    )
    basis: str = Field(description="gtin, brand_mpn, title_code or title_words.")
    key: str = Field(description="The value the offers share: GTIN-14, brand:mpn, model code or word set.")
    sources: list[str] = Field(description="Shops in this model, at least two.")
    cheapest: MarketOffer = Field(description="Lowest everyday price within the model.")
    price_spread_rub: float = Field(description="Highest minus lowest price within the model.")
    offers: list[MarketOffer] = Field(description="The model's offers, cheapest first.")


class ModelCompareResponse(CompareResponse):
    """A price comparison plus the models found in more than one shop."""

    models: list[ModelGroup] = Field(
        default_factory=list,
        description=(
            "One model from several shops, strongest evidence first. New goods priced in roubles only: "
            "Avito ads, used or display units, accessories and deal posts are left out."
        ),
    )
