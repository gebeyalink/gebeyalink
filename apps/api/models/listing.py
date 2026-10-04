"""The listings a seller puts up, and the photographs that go with them.

A listing is a promise: this much of this crop, at this price, in this place, until this
moment. Everything in this module exists to keep that promise legible and to keep a row from
being quietly edited into a different promise than the one that was made.

Three things are deliberate here and are worth stating rather than leaving to be inferred.

The short code is six Crockford Base32 characters with no prefix. Orders carry `GL-` and a
listing does not, so the two can be told apart by length instead of by having to strip a
marker off one of them, and the code a trader reads on an SMS is the code a buyer reads on a
listing page. Crockford's alphabet leaves out I, L, O and U so that a hand-keyed code has no
way of being misread as a different code.

The status is a real enum rather than free text, because a status nobody can spell wrong is
a status whose meaning cannot drift.

The photographs are stored as URLs and are not fetched, resized or inspected here. Their
shape is checked where it is written, and what is kept is what the caller said the picture
is at. There is no object storage in this slice, so a URL is recorded, not verified.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base, TimestampMixin
from apps.api.db.enums import stored_values


class ListingSource(enum.StrEnum):
    """Which door the listing came in through.

    The same field is filled by four different clients with four different vocabularies, and
    the record of which is what makes a support question answerable later.
    """

    APP = "APP"
    USSD = "USSD"
    SMS = "SMS"
    AGENT = "AGENT"


class DeliveryOption(enum.StrEnum):
    """How the buyer may take the crop away.

    `SELLER_DELIVERY` is the one a fee attaches to. `TRANSPORT_PARTNER` means the buyer
    arranges it themselves, so there is no fee for us to hold.
    """

    PICKUP = "PICKUP"
    SELLER_DELIVERY = "SELLER_DELIVERY"
    TRANSPORT_PARTNER = "TRANSPORT_PARTNER"


class ListingStatus(enum.StrEnum):
    """Where a listing sits in its life.

    The transitions between these are not free; they are the state table, and one guard in
    the service layer decides which of them are allowed.
    """

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    SOLD_OUT = "SOLD_OUT"
    EXPIRED = "EXPIRED"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"


class Listing(TimestampMixin, Base):
    """One seller offering one crop at a place and a time."""

    __tablename__ = "listings"

    # A lifecycle move updates the row, and `updated_at` is written by the database clock. With
    # this set, that value comes back in the same statement instead of being left to load on
    # first read, which in an async request would be an un-awaited query.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The account that wrote it. An agent may write on another's behalf, which is who they
    # wrote it as, so both are held and neither is guessed from the other.
    seller_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", name="fk_listings_seller_id_users"),
        nullable=False,
    )
    on_behalf_of_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", name="fk_listings_on_behalf_of_user_id_users"),
        nullable=True,
    )
    source: Mapped[ListingSource] = mapped_column(
        SqlEnum(ListingSource, name="listing_source", values_callable=stored_values),
        nullable=False,
    )
    # The taxonomy's own number, so a listing and a published crop are the same thing rather
    # than two rows that happen to agree.
    crop_id: Mapped[int] = mapped_column(
        SmallInteger(),
        ForeignKey("crops.id", name="fk_listings_crop_id_crops"),
        nullable=False,
    )
    quantity_kg: Mapped[int] = mapped_column(Integer(), nullable=False)
    # Weight already consumed against this listing. Carried because an order settles
    # against it; nothing in this slice moves it.
    sold_kg: Mapped[int] = mapped_column(Integer(), server_default="0", nullable=False)
    min_order_kg: Mapped[int] = mapped_column(Integer(), nullable=False)
    # Santim, as money is held everywhere else here: an integer, never a float, so no
    # rounding question can arise from the arithmetic.
    unit_price_santim: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    harvest_date: Mapped[date] = mapped_column(Date(), nullable=False)
    # When it becomes buyable, as distinct from when it was posted. Null means immediately.
    available_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the promise lapses. Read against the clock on every read, so a listing cannot be
    # left standing past this by nobody happening to look at it.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    region_code: Mapped[str] = mapped_column(
        String(length=2),
        ForeignKey("regions.code", name="fk_listings_region_code_regions"),
        nullable=False,
    )
    woreda_id: Mapped[int] = mapped_column(
        Integer(),
        ForeignKey("woredas.id", name="fk_listings_woreda_id_woredas"),
        nullable=False,
    )
    delivery_options: Mapped[list[DeliveryOption]] = mapped_column(
        postgresql.ARRAY(
            SqlEnum(DeliveryOption, name="delivery_option", values_callable=stored_values)
        ),
        nullable=False,
    )
    delivery_fee_santim: Mapped[int] = mapped_column(
        BigInteger(), server_default="0", nullable=False
    )
    organic: Mapped[bool] = mapped_column(Boolean(), server_default="false", nullable=False)
    description: Mapped[str | None] = mapped_column(String(length=500), nullable=True)
    status: Mapped[ListingStatus] = mapped_column(
        SqlEnum(ListingStatus, name="listing_status", values_callable=stored_values),
        nullable=False,
    )
    # A price may be flagged and still be posted. This warns the seller and marks the listing
    # for whoever reads it; it is never a refusal, and nothing here blocks on it.
    price_outlier: Mapped[bool] = mapped_column(Boolean(), server_default="false", nullable=False)
    short_code: Mapped[str] = mapped_column(String(length=8), nullable=False, unique=True)
    listing_attributes: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB(astext_type=Text()),
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer(), server_default="0", nullable=False)


class ListingPhoto(TimestampMixin, Base):
    """One photograph a seller attached, at a position in the set.

    The position is what orders the set, and it is unique per listing: two photographs
    claiming the same place would make the order a guess.
    """

    __tablename__ = "listing_photos"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    listing_id: Mapped[UUID] = mapped_column(
        ForeignKey("listings.id", name="fk_listing_photos_listing_id_listings"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(Text(), nullable=False)
    width: Mapped[int] = mapped_column(Integer(), nullable=False)
    height: Mapped[int] = mapped_column(Integer(), nullable=False)
    sort_order: Mapped[int] = mapped_column(SmallInteger(), nullable=False)
