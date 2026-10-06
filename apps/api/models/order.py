"""The order: a buyer's promise to pay for a seller's kilograms.

An order is the only row in the platform where two parties, a weight, and money meet. It is
created in one transaction with the reservation that holds its weight and the offer it came from,
because an order without either of those is a claim on stock that nothing owns.

Three decisions are worth stating rather than leaving to be inferred.

The status is not a field a request may set. There is exactly one function in the service layer
that writes an order's status, it consults the state table before it does, and creation goes
through it like every other move: an order is born `CREATED` and enters `AWAITING_FUNDING` in
the same transaction that reserves its weight, because that transition is what makes the
reservation mean something. Nothing outside the guard can write a status, so an endpoint cannot
drift into permitting something the table forbids.

The money columns are the frozen output of the shared money functions and nothing else. Whatever
a client sends is ignored, because a total that arrived in the request is a total the seller did
not agree to. `fee_payer` is snapshotted for the same reason the totals are frozen: it is a
configuration value at the moment of sale, and an order that re-read it later would be settled
under a policy nobody agreed to when the kilograms changed hands.

The short code is `GL-` and six Crockford Base32 characters, stored with the prefix. It is
stored verbatim because everything that shows a code to a human - the app, the SMS, the USSD
menu - must show the same nine characters, and stripping a marker off the stored value is how
two of those three end up disagreeing. A listing's code is six characters with no prefix, so the
two can be told apart by shape alone.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base, TimestampMixin
from apps.api.db.enums import stored_values
from apps.api.models.listing import DeliveryOption
from packages.shared.money import FeePayer


class OrderStatus(enum.StrEnum):
    """Where an order sits in its life.

    The transitions between these are not free: they are the state table in the service layer,
    and one guard decides which of them are allowed. `CANCELLED_REFUNDED` is distinct from
    `CANCELLED` because a cancelled order never took the buyer's money while a refunded one did,
    and an account statement that could not tell those apart would be no statement at all.
    """

    CREATED = "CREATED"
    AWAITING_FUNDING = "AWAITING_FUNDING"
    CONFIRMED = "CONFIRMED"
    FUNDED = "FUNDED"
    READY_FOR_PICKUP = "READY_FOR_PICKUP"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    CANCELLED_REFUNDED = "CANCELLED_REFUNDED"
    DISPUTED = "DISPUTED"


class CancelReason(enum.StrEnum):
    """Why an order stopped without anybody deciding to stop it, or deciding to.

    `FUNDING_EXPIRED` is the one the buyer never sees coming and the one that must be
    distinguishable forever after: it is the only way to tell an order that fell off the end of
    its funding window from one a party walked away from, and those two mean very different
    things to the next person who looks at that buyer's history.

    The four values are the whole vocabulary the API contract gives. This slice writes two of
    them — a buyer's cancellation and a lapsed funding window — and the other two exist because
    the vocabulary is one list rather than one per slice, so that a reader of an order is never
    guessing which words might turn up there.
    """

    FUNDING_EXPIRED = "FUNDING_EXPIRED"
    BUYER_CANCELLED = "BUYER_CANCELLED"
    SELLER_DELAYED = "SELLER_DELAYED"
    OTHER = "OTHER"


class PaymentMode(enum.StrEnum):
    """Whether the platform holds the buyer's money until delivery, or never sees it.

    COD orders never move money: both parties confirm the terms instead of the buyer funding
    them, and the order stays `CREATED` until that happens. Escrow orders are born
    `AWAITING_FUNDING` and stay there until payment arrives. Which settlement path an order
    is on is decided by this column, so a reader never has to guess from a missing value.
    """

    ESCROW = "ESCROW"
    COD = "COD"


class Order(TimestampMixin, Base):
    """One buyer, one seller, one weight of one listing, and the money both of them agreed."""

    __tablename__ = "orders"

    # A lifecycle move updates the row, and `updated_at` is written by the database clock. With
    # this set, that value comes back in the same statement instead of being left to load on
    # first read, which in an async request would be an un-awaited query.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Stored with its prefix, and shown everywhere exactly as stored. The nine characters are
    # the whole handle: an SMS that says `GL-4K2M9Q` and an API that says `4K2M9Q` are two
    # different codes as far as a human holding a handset is concerned.
    short_code: Mapped[str] = mapped_column(String(length=9), nullable=False, unique=True)
    buyer_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", name="fk_orders_buyer_id_users"),
        nullable=False,
    )
    # Held separately from `listing_id`'s seller rather than read through the listing, because
    # the order is the record of who owed whom on the day it was made. A listing's seller is a
    # column that an on-behalf-of arrangement can make ambiguous, and an order is exactly where
    # that ambiguity must not be.
    seller_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", name="fk_orders_seller_id_users"),
        nullable=False,
    )
    listing_id: Mapped[UUID] = mapped_column(
        ForeignKey("listings.id", name="fk_orders_listing_id_listings"),
        nullable=False,
    )
    # The offer this came from, or null when the buyer paid the asking price without bidding.
    # Null is meaningful rather than missing: a buy-now order and an accepted offer are the same
    # order to everyone downstream, and what distinguishes them is whether anybody negotiated.
    offer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("offers.id", name="fk_orders_offer_id_offers"),
        nullable=True,
    )
    quantity_kg: Mapped[int] = mapped_column(Integer(), nullable=False)
    # Santim, always: an integer, so no rounding question can arise from the arithmetic.
    unit_price_santim: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    delivery_method: Mapped[DeliveryOption] = mapped_column(
        SqlEnum(DeliveryOption, name="delivery_option", values_callable=stored_values),
        nullable=False,
    )
    # The four money figures below are what the shared money functions returned, frozen at
    # creation. They are not recomputed on read, because a total that can change under a reader
    # is not a total anybody signed.
    delivery_fee_santim: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    platform_fee_santim: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    vat_santim: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    # What the buyer pays in total, delivery and platform fee included. That it is the buyer's
    # figure and not the platform's is why it is named `total` and not `subtotal`.
    total_santim: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    # Which side was funding the fee when this order was made. Snapshotted, never read from
    # configuration again: a settlement months later must follow the policy in force at the sale.
    fee_payer: Mapped[FeePayer] = mapped_column(
        SqlEnum(FeePayer, name="fee_payer", values_callable=stored_values),
        nullable=False,
    )
    # Where the goods are going. Not optional even for a pickup, because the column list does not
    # offer a null case and a seller delivering a truckload still needs to write down where it is
    # going; the USSD slice reads these from the seller's own profile rather than asking.
    buyer_address_woreda_id: Mapped[int] = mapped_column(
        ForeignKey("woredas.id", name="fk_orders_buyer_address_woreda_id_woredas"),
        nullable=False,
    )
    buyer_address_text: Mapped[str] = mapped_column(String(length=300), nullable=False)
    # Six digits, shown to the buyer and read back by the seller to confirm a pickup. Generated
    # for every order whether or not it will ever be needed, because a code that exists only on
    # the orders that happen to be picked up is a code that is missing on the one that is.
    pickup_code: Mapped[str] = mapped_column(String(length=6), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        SqlEnum(OrderStatus, name="order_status", values_callable=stored_values),
        nullable=False,
    )
    # Where the order was before somebody disputed it, so that rejecting the dispute puts it back
    # where it was. Null whenever no dispute is open, and not derived from `status`: an order that
    # is `DISPUTED` says nothing on its own about what it was.
    state_before_dispute: Mapped[OrderStatus | None] = mapped_column(
        SqlEnum(OrderStatus, name="order_status", values_callable=stored_values),
        nullable=True,
    )
    funded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # What actually arrived, which is not always what was ordered. Null until delivery.
    delivered_weight_kg: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(length=40), nullable=True)
    payment_mode: Mapped[PaymentMode] = mapped_column(
        SqlEnum(PaymentMode, name="payment_mode", values_callable=stored_values),
        server_default=PaymentMode.ESCROW.value,
        nullable=False,
    )
    # When both parties had confirmed a COD order (FR-2102). Null on every escrow order, which
    # is confirmed by payment rather than by a second signature. The seller-delayed window is
    # measured from this instant by the read path, lazily.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The first instant each party confirmed, kept apart so that confirming is idempotent per
    # party: a party's replay of their own confirmation changes nothing, and the order only
    # reaches `CONFIRMED` once both stamps are present. Either party may be first.
    buyer_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    seller_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Carried because §11.1 gives every mutable business entity one. Nothing edits an order's
    # terms; a change of terms is a new order, which is what a counter and a new offer are.
    version: Mapped[int] = mapped_column(Integer(), server_default="0", nullable=False)
