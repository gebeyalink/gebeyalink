"""Request and response bodies for a listing.

Two things are worth knowing about these shapes before changing them.

The response carries a place and nothing finer. ``region_code`` and ``woreda_id`` are what a
buyer needs to judge whether they can collect the crop; the trader's own GPS point is held
for their benefit and is not published, so it has no field here. Leaving it off is the
guarantee, and adding one later would be a decision rather than an oversight.

Photographs are URLs and only URLs. The rules about what a JPEG must weigh, how wide it
must be and how far it is downscaled all belong to the moment a file is fetched and stored,
which is not this slice. So a photo here is a position and a reference, and the listing's
own limits — at most five, in order — are the ones enforced.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from apps.api.models.listing import DeliveryOption, ListingStatus

#: At most this many photographs, per the listing's own field rule. The size and dimension
#: rules that sit alongside it are the storage layer's to enforce.
MAX_PHOTOS = 5

#: Long enough for a signed URL with a query string, short enough that a whole URL cannot be
#: smuggled into a column meant for one.
MAX_PHOTO_URL_LENGTH = 2048


class PhotoInput(BaseModel):
    """One photograph a seller is attaching."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=MAX_PHOTO_URL_LENGTH)
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class PhotoResponse(PhotoInput):
    """A photograph as it is read back, with the place it holds in the set."""

    sort_order: int = Field(ge=0)


class ListingCreateBody(BaseModel):
    """A listing as a seller states it.

    Everything here is what the seller knows at the moment of posting. What the platform
    adds around it — the source it arrived through, the status it starts in, the short code
    it is quoted by — is not accepted from the client, because a client that could set its
    own status could also set a listing that was never reviewed.

    ``expires_at`` is optional because a listing that nobody thought about should still be
    postable: thirty days is the default the specification gives.
    """

    model_config = ConfigDict(extra="forbid")

    crop_id: int = Field(ge=1)
    quantity_kg: int = Field(ge=1, le=1_000_000)
    min_order_kg: int = Field(ge=1)
    unit_price_santim: int = Field(gt=0)
    harvest_date: date
    available_from: datetime | None = None
    expires_at: datetime | None = None
    region_code: str = Field(min_length=2, max_length=2)
    woreda_id: int = Field(ge=1)
    delivery_options: list[DeliveryOption] = Field(min_length=1)
    delivery_fee_santim: int = Field(default=0, ge=0)
    description: str | None = Field(default=None, max_length=500)
    organic: bool = False
    photos: list[PhotoInput] = Field(default_factory=list, max_length=MAX_PHOTOS)
    listing_attributes: dict[str, Any] | None = None


class ListingUpdateBody(BaseModel):
    """A change to some of a seller's own listing.

    A merge, so an absent field means leave it alone. That is why a nullable field cannot be
    unset by omitting it: clearing one is an explicit value, and this slice does not offer a
    way to tell "absent" from "empty" for text. ``version`` is required, because an edit
    that is not sure what it is editing is how two phones overwrite each other.

    Not editable here, deliberately: ``status`` moves only through the lifecycle endpoints,
    ``short_code`` is quoted to buyers already and cannot be changed under them, ``sold_kg``
    is written by orders rather than by their seller, and the crop, the region and the woreda
    are what the listing is an offer *of*, *where* and *in*. Those four have no field here at
    all rather than a field that is quietly ignored, so a client that tries finds out.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    quantity_kg: int | None = Field(default=None, ge=1, le=1_000_000)
    min_order_kg: int | None = Field(default=None, ge=1)
    unit_price_santim: int | None = Field(default=None, gt=0)
    harvest_date: date | None = None
    available_from: datetime | None = None
    expires_at: datetime | None = None
    delivery_options: list[DeliveryOption] | None = Field(default=None, min_length=1)
    delivery_fee_santim: int | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=500)
    organic: bool | None = None
    photos: list[PhotoInput] | None = Field(default=None, max_length=MAX_PHOTOS)
    listing_attributes: dict[str, Any] | None = None


class ListingResponse(BaseModel):
    """A listing as it is read back.

    The seller's own point on the map is not here, and neither is which account an agent
    wrote on another's behalf. Everything published about a listing is what a buyer needs in
    order to decide whether to buy it.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    short_code: str
    crop_id: int
    quantity_kg: int
    sold_kg: int
    min_order_kg: int
    unit_price_santim: int
    harvest_date: date
    available_from: datetime | None
    expires_at: datetime
    region_code: str
    woreda_id: int
    delivery_options: list[DeliveryOption]
    delivery_fee_santim: int
    organic: bool
    description: str | None
    photos: list[PhotoResponse] = Field(default_factory=list)
    status: ListingStatus
    price_outlier: bool
    listing_attributes: dict[str, Any] = Field(default_factory=dict)
    version: int
    created_at: datetime
    updated_at: datetime
