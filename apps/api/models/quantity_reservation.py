"""Reserved kilograms: the reason availability is derived rather than counted.

A listing says it has a thousand kilos. What it has *left* is a question about orders, not about
the listing: a hundred kilos are held by an order nobody has paid for yet, three hundred are
converted because the money is in escrow, and ninety were delivered. Those live here rather than
in a counter on the listing row, because a counter is a number that has to be updated by every
path that could change it, and the first path nobody remembered is the one that oversells.

`holder_type` is an enum with one member and a foreign key behind `holder_id`. That looks
redundant, and is not: the column narrows in version 2.1.3 so that the only thing that can hold
kilograms is an order, and the foreign key is what makes that true rather than hopeful. If a
dispute ever froze stock without an order behind it, this is the column that would have to
change, and it is better to find that out at a constraint than in a reconciliation.

Delivered stock never comes back. `CONSUMED` is a state a row never leaves, which is what stops a
delivered kilo from re-entering availability and being sold twice. `RELEASED` is the only way
out of a hold, and it means the deal did not happen rather than that the goods moved.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum as SqlEnum
from sqlalchemy import ForeignKey, Integer
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base, TimestampMixin
from apps.api.db.enums import stored_values


class HolderType(enum.StrEnum):
    """What kind of thing is holding kilograms.

    One member today, and kept as an enum rather than dropped because the column is the place a
    second holder would be recorded, and adding one later to a table of weights is a migration
    rather than an edit.
    """

    ORDER = "ORDER"


class ReservationState(enum.StrEnum):
    """How far along the deal a held weight is.

    The progression is `HELD` then one of `CONVERTED` (the money arrived, so the weight is sold
    but not yet moved), `RELEASED` (the deal fell through, so the weight is sellable again), or
    `CONSUMED` (delivered, so the weight is gone for good).
    """

    HELD = "HELD"
    CONVERTED = "CONVERTED"
    RELEASED = "RELEASED"
    CONSUMED = "CONSUMED"


#: The states whose weight is unavailable to a new buyer. `RELEASED` is absent and that is the
#: whole point of it: a released reservation is how a kilogram becomes sellable again.
BLOCKING_STATES: tuple[ReservationState, ...] = (
    ReservationState.HELD,
    ReservationState.CONVERTED,
    ReservationState.CONSUMED,
)


class QuantityReservation(TimestampMixin, Base):
    """Some kilograms of one listing, held for one order."""

    __tablename__ = "quantity_reservations"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listings.id", name="fk_quantity_reservations_listing_id_listings"),
        nullable=False,
    )
    holder_type: Mapped[HolderType] = mapped_column(
        SqlEnum(HolderType, name="holder_type", values_callable=stored_values),
        nullable=False,
    )
    # The order holding these kilos. A foreign key rather than a bare uuid, because
    # `holder_type` says what kind of holder this is and this says which one.
    holder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", name="fk_quantity_reservations_holder_id_orders"),
        nullable=False,
    )
    quantity_kg: Mapped[int] = mapped_column(Integer(), nullable=False)
    state: Mapped[ReservationState] = mapped_column(
        SqlEnum(ReservationState, name="reservation_state", values_callable=stored_values),
        nullable=False,
    )
