"""Request and response bodies for a trader's saved searches.

The body is the search filter set plus the two digest choices, which is exactly the
``SavedSearch`` shape ``docs/openapi.yaml`` publishes. The database row carries two fields
the contract does not: ``user_id``, which is the token rather than a body field, and
``last_digest_at``, which is the worker's bookkeeping. The schemas are where that difference
is kept, so neither can leak into a response by being added to a model.

``digest_channel`` is a two-word enum here even though the storage type has a third member.
The contract admits only PUSH and SMS, and a saved search cannot ask for a rail the API has
no way to promise.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from apps.api.models.listing import DeliveryOption
from apps.api.models.user import VerificationTier


class DigestChannel(StrEnum):
    """The two channels a saved-search digest may name in the published contract."""

    PUSH = "PUSH"
    SMS = "SMS"


class SavedSearchBody(BaseModel):
    """One saved search as a client writes it."""

    model_config = ConfigDict(extra="forbid")

    crop_id: int | None = Field(default=None, ge=1)
    region_code: str | None = Field(default=None, min_length=2, max_length=2)
    woreda_id: int | None = Field(default=None, ge=1)
    radius_km: int | None = Field(default=None, ge=1, le=500)
    price_min: int | None = Field(default=None, ge=0)
    price_max: int | None = Field(default=None, ge=0)
    harvest_date_from: date | None = None
    harvest_date_to: date | None = None
    organic: bool | None = None
    delivery_option: DeliveryOption | None = None
    seller_tier_min: VerificationTier | None = None
    digest: bool = True
    digest_channel: DigestChannel = DigestChannel.SMS


class SavedSearchResponse(BaseModel):
    """One saved search as the published contract describes it."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    crop_id: int | None
    region_code: str | None
    woreda_id: int | None
    radius_km: int | None
    price_min: int | None
    price_max: int | None
    harvest_date_from: date | None
    harvest_date_to: date | None
    organic: bool | None
    delivery_option: DeliveryOption | None
    seller_tier_min: VerificationTier | None
    digest: bool
    digest_channel: DigestChannel
