"""Quantity reservation repository.

What is left of a listing is a sum over this table and nothing else. There is no counter of
remaining kilograms on the listing row, and that absence is the design: a counter is a number
that every path able to change the answer has to remember to update, and the first path somebody
forgets is the one that oversells a seller's stock. Deriving the answer means the only way to be
wrong is to be wrong about a sum, which is arithmetic nobody has to maintain by hand.

The blocking states are named in one place, `BLOCKING_STATES`, and every query that asks "what is
still spoken for" filters on it. A new reservation state therefore either blocks stock or does
not, and that decision is made once in the model rather than separately in each query that
happens to care.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.order import Order
from apps.api.models.quantity_reservation import (
    BLOCKING_STATES,
    QuantityReservation,
    ReservationState,
)


@dataclass(frozen=True, slots=True)
class HeldWeight:
    """One order's claim on a listing's stock, and which order that is."""

    order: Order
    reservation: QuantityReservation


class ReservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, reservation: QuantityReservation) -> QuantityReservation:
        """Add a reservation and give it its identity, without making the write permanent."""
        self._session.add(reservation)
        await self._session.flush()
        return reservation

    async def reserved_kg_for_listing(self, listing_id: uuid.UUID) -> int:
        """Every kilogram of this listing that is spoken for, whatever state the order is in.

        Read once per sale, under the listing's row lock, so the number it returns cannot change
        between being computed and being acted on.
        """
        stmt = select(func.coalesce(func.sum(QuantityReservation.quantity_kg), 0)).where(
            QuantityReservation.listing_id == listing_id,
            QuantityReservation.state.in_(BLOCKING_STATES),
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def reserved_kg_for_listings(self, listing_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Spoken-for kilograms for several listings at once, keyed by listing.

        One statement for the whole batch, because availability is read on the accept path and on
        the listing page, and both of those are places where a query per listing is a slow
        request rather than a fast one.
        """
        if not listing_ids:
            return {}
        stmt = (
            select(QuantityReservation.listing_id, func.sum(QuantityReservation.quantity_kg))
            .where(
                QuantityReservation.listing_id.in_(listing_ids),
                QuantityReservation.state.in_(BLOCKING_STATES),
            )
            .group_by(QuantityReservation.listing_id)
        )
        result = await self._session.execute(stmt)
        return {listing_id: int(total) for listing_id, total in result.all()}

    async def blocking_for_listing(self, listing_id: uuid.UUID) -> list[HeldWeight]:
        """Every order currently holding weight on this listing, oldest claim first.

        Read to name the order that stopped a listing edit, and to convert held kilos when
        escrow arrives. Both need to know *which* orders are in the way, not merely how much in
        total, so the reservations are joined to the orders that own them.
        """
        stmt = (
            select(Order, QuantityReservation)
            .join(QuantityReservation, QuantityReservation.holder_id == Order.id)
            .where(
                QuantityReservation.listing_id == listing_id,
                QuantityReservation.state.in_(BLOCKING_STATES),
            )
            .order_by(QuantityReservation.created_at, QuantityReservation.id)
        )
        result = await self._session.execute(stmt)
        return [HeldWeight(order=row[0], reservation=row[1]) for row in result.all()]

    async def release_for_order(self, order_id: uuid.UUID) -> int:
        """Give back every kilogram this order still holds, and say how many that was.

        Bulk update rather than a loop, and deliberately unconditional about which blocking
        state the reservation was in. An order that was paid for and then cancelled has to give
        the kilos back just as one that never was paid for does - the difference between those
        two is what happens to the money, which is not this table's business - and a filter that
        mentioned `HELD` alone would quietly keep sold stock reserved forever.
        """
        stmt = (
            update(QuantityReservation)
            .where(
                QuantityReservation.holder_id == order_id,
                QuantityReservation.state.in_(BLOCKING_STATES),
            )
            .values(state=ReservationState.RELEASED)
            .returning(QuantityReservation.quantity_kg)
        )
        result = await self._session.execute(stmt)
        return sum(int(quantity) for quantity in result.scalars().all())

    async def convert_for_order(self, order_id: uuid.UUID) -> int:
        """Turn this order's held kilos into converted ones, and say how many that was.

        Called when the money arrives, and it is the step that makes a hold permanent: after it,
        the kilos are `CONVERTED` rather than `HELD` and are still unavailable, but they no longer
        depend on anyone remembering to pay.
        """
        stmt = (
            update(QuantityReservation)
            .where(
                QuantityReservation.holder_id == order_id,
                QuantityReservation.state == ReservationState.HELD,
            )
            .values(state=ReservationState.CONVERTED)
            .returning(QuantityReservation.quantity_kg)
        )
        result = await self._session.execute(stmt)
        return sum(int(quantity) for quantity in result.scalars().all())

    async def consume_for_order(self, order_id: uuid.UUID) -> int:
        """Record that this order's kilos were delivered, and say how many that was.

        The one move that is not reversible. `CONSUMED` is not a blocking state by accident but
        by design, and the availability formula includes it so that delivered weight stays
        deducted from the listing forever, which is what "delivered" has to mean for the
        seller's remaining stock to mean anything.
        """
        stmt = (
            update(QuantityReservation)
            .where(
                QuantityReservation.holder_id == order_id,
                QuantityReservation.state.in_((ReservationState.HELD, ReservationState.CONVERTED)),
            )
            .values(state=ReservationState.CONSUMED)
            .returning(QuantityReservation.quantity_kg)
        )
        result = await self._session.execute(stmt)
        return sum(int(quantity) for quantity in result.scalars().all())

    async def commit(self) -> None:
        await self._session.commit()

    async def flush(self) -> None:
        await self._session.flush()
