"""Request DTOs. Validation only: business rules stay in procurement_core.

Quantities and money arrive as JSON strings. A JSON number would already have passed
through an IEEE-754 double before Pydantic ever saw it, so rejecting float is the last
point where BR-03's exactness can still be enforced.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from procurement_core.offers import Offer, PriceBreak


def _reject_float(value: object) -> object:
    if isinstance(value, float):
        # ValueError, not TypeError: Pydantic converts only ValueError and AssertionError
        # into a ValidationError. A TypeError would escape the validator as a 500.
        raise ValueError(  # noqa: TRY004
            "Send exact numbers as JSON strings; floats lose precision (BR-03)")
    return value


Exact = Annotated[Decimal, BeforeValidator(_reject_float)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemandCreate(Strict):
    project_id: str = Field(min_length=1)
    product_version: str = Field(min_length=1)
    production_qty: Exact
    need_by_date: date
    created_by: str = Field(min_length=1)
    selected: dict[str, str] = Field(default_factory=dict)
    tenant_id: str = "default"
    is_simulated: bool = False


class RunCreate(Strict):
    demand_id: UUID
    trigger_kind: Literal["user", "monitor"] = "user"
    budget_total: Exact | None = None
    parent_run_id: UUID | None = None


class PriceBreakIn(Strict):
    min_qty: Exact
    unit_price: Exact

    def to_domain(self) -> PriceBreak:
        return PriceBreak(min_qty=self.min_qty, unit_price=self.unit_price)


class OfferIn(Strict):
    component_id: str = Field(min_length=1)
    distributor: str = Field(min_length=1)
    distributor_sku: str = Field(min_length=1)
    region: str = Field(min_length=1)
    packaging: str = Field(min_length=1)
    evidence_ref: str = Field(min_length=1)
    retrieved_at: datetime
    moq: Exact
    order_multiple: Exact
    currency: str | None = None
    price_breaks: list[PriceBreakIn] = Field(default_factory=list)
    stock_qty: Exact | None = None
    lead_time_days: int | None = None
    identity_status: str = "source_asserted"
    is_simulated: bool = False

    @field_validator("retrieved_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        # A naive timestamp cannot be compared against a quote's validity window.
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retrieved_at must include a timezone offset")
        return value

    def to_domain(self) -> Offer:
        return Offer(
            component_id=self.component_id,
            distributor=self.distributor,
            distributor_sku=self.distributor_sku,
            region=self.region,
            packaging=self.packaging,
            currency=self.currency,
            retrieved_at=self.retrieved_at,
            moq=self.moq,
            order_multiple=self.order_multiple,
            price_breaks=tuple(tier.to_domain() for tier in self.price_breaks),
            stock_qty=self.stock_qty,
            lead_time_days=self.lead_time_days,
            identity_status=self.identity_status,
            evidence_ref=self.evidence_ref,
            is_simulated=self.is_simulated,
        )


class PlanCreate(Strict):
    snapshot_ids: list[UUID] = Field(min_length=1)
    #: Keyed by component_id. Absent means "no purchase required"; a positive shortage
    #: without an offer is rejected downstream by BR-06/BR-07, not here.
    offers: dict[str, OfferIn] = Field(default_factory=dict)

    def domain_offers(self) -> dict[str, Offer]:
        return {component: offer.to_domain() for component, offer in self.offers.items()}
