"""Order repository.

Every read of an order that a person or a rule acts on is done by primary key or by party, and
never by short code. A short code is a handle for a human to say out loud, not an identifier the
application trusts, and a repository that could look an order up by it would eventually be asked
to look one up by something typed from a text message.

There is exactly one exception, and it is the sentence above arriving at last: the SMS channel
has nowhere else to put an order than its short code, so it gets one read by code —
``find_by_short_code_for_party`` — and the lookup is scoped to a caller who is already a party.
The code narrows which of *that caller's own* orders is meant; it never grants access to one.

The two locked reads exist because two rules have to read an order and then act on what they
read. Accepting an offer reads the listing, not the order; cancelling reads the order it is
about to cancel. Both are `FOR UPDATE` because "read the order, check it can move, move it" is
three steps, and two concurrent cancels of the same order would otherwise both read
`AWAITING_FUNDING` and both believe they were the one to release the kilos.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from sqlalchemy import Date, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.crop import Crop
from apps.api.models.listing import Listing
from apps.api.models.order import Order, OrderStatus
from apps.api.models.quantity_reservation import QuantityReservation

#: The states an order can be in while somebody's money is genuinely at stake: the platform
#: has taken the money for it and is holding or spending it on the trade's behalf. An order
#: reaches one of these only by being funded first. Cash on delivery has no such moment —
#: the platform never holds the money, the buyer hands it over at the door — so a COD order
#: never stands in one of these states and never speaks to the price board.
_FUNDED_STATUSES: Final[frozenset[OrderStatus]] = frozenset(
    {
        OrderStatus.FUNDED,
        OrderStatus.READY_FOR_PICKUP,
        OrderStatus.SHIPPED,
        OrderStatus.DELIVERED,
        OrderStatus.COMPLETED,
        OrderStatus.DISPUTED,
    }
)


@dataclass(frozen=True, slots=True)
class OrderPage:
    """One page of orders and whether more remain."""

    orders: list[Order]
    has_more: bool


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, order: Order) -> Order:
        """Add an order and give it its identity, without making the write permanent."""
        self._session.add(order)
        await self._session.flush()
        return order

    async def find_by_id(self, order_id: uuid.UUID) -> Order | None:
        """One order by its internal identity."""
        stmt = select(Order).where(Order.id == order_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_by_id_for_update(self, order_id: uuid.UUID) -> Order | None:
        """One order by its internal identity, with the row locked until this transaction ends.

        Used where the rule being applied is about the order's own state - cancelling it,
        expiring its funding window - so that the state read here and the state written after it
        cannot come from two different moments.
        """
        stmt = select(Order).where(Order.id == order_id).with_for_update()
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_by_id_for_update_skip_locked(self, order_id: uuid.UUID) -> Order | None:
        """One order by its internal identity, locked if it is free and skipped if it is not.

        The worker's road rather than a request's, and the difference is the same one the
        acceptance flow draws: a sweep has many orders to settle and nothing to refuse, so an
        order another settler already holds is passed over and picked up by the next sweep
        rather than waited on. Absent means somebody else has it, or it went away.
        """
        stmt = select(Order).where(Order.id == order_id).with_for_update(skip_locked=True)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def short_code_exists(self, short_code: str) -> bool:
        """Whether an order already carries this code.

        Asked before writing rather than caught afterwards, so a collision is an ordinary retry
        rather than a failed transaction the caller has to see. The unique constraint underneath
        is still what makes it true; this only makes hitting it unlikely enough to read as an
        ordinary event rather than a bug.
        """
        stmt = select(func.count()).select_from(Order).where(Order.short_code == short_code)
        result = await self._session.execute(stmt)
        return bool(result.scalar_one())

    async def find_by_short_code_for_party(
        self, short_code: str, party_id: uuid.UUID
    ) -> Order | None:
        """One of this party's own orders, named by the code they read aloud.

        The one read by short code, and it is scoped rather than general: the caller must
        already be the buyer or the seller, so the code can only narrow which of their orders
        is meant, never reach somebody else's. Normalised to the stored upper case because a
        handset may send either.
        """
        stmt = select(Order).where(
            Order.short_code == short_code.strip().upper(),
            or_(Order.buyer_id == party_id, Order.seller_id == party_id),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def page_for_party(
        self,
        party_id: uuid.UUID,
        *,
        buyer: bool,
        status: OrderStatus | None,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> OrderPage:
        """One page of the orders a party is on one side of, newest first.

        Newest first, because both readers arrive with a question about now: a buyer checking
        whether they have paid for something, a seller checking what is waiting to be handed
        over. The page is cut at ``limit + 1`` so that "is there another" is answered by the
        rows themselves rather than by a second count query.
        """
        party_column = Order.buyer_id if buyer else Order.seller_id
        stmt = select(Order).where(party_column == party_id)
        if status is not None:
            stmt = stmt.where(Order.status == status)
        if after is not None:
            after_created_at, after_id = after
            stmt = stmt.where(tuple_(Order.created_at, Order.id) < (after_created_at, after_id))
        stmt = stmt.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit + 1)
        result = await self._session.execute(stmt)
        rows = list(result.scalars().all())
        return OrderPage(orders=rows[:limit], has_more=len(rows) > limit)

    async def list_unfunded_past_on_listing(
        self, listing_id: uuid.UUID, cutoff: datetime, limit: int
    ) -> list[Order]:
        """Orders on this listing still waiting for money whose window closed before `cutoff`.

        Asked against `created_at` because that is when the order entered `AWAITING_FUNDING`: an
        order is created and funding-waiting in the same transaction, so the two moments are the
        same moment, and a separate timestamp column would only ever hold a duplicate of it.

        Scoped to one listing because that is the only way this is asked for today: a sale
        sweeps the holds that could be standing in its way before it counts them. Scoped and
        bounded rather than global, because a marketplace whose buyers abandoned a thousand
        orders overnight should find them all, and should find them across several sweeps rather
        than inside one request that times out.
        """
        stmt = (
            select(Order)
            .join(QuantityReservation, QuantityReservation.holder_id == Order.id)
            .where(
                Order.status == OrderStatus.AWAITING_FUNDING,
                Order.created_at < cutoff,
                QuantityReservation.listing_id == listing_id,
            )
            .order_by(Order.created_at, Order.id)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_lapsed_shipment_ids(self, now: datetime, limit: int) -> list[uuid.UUID]:
        """Shipped orders whose crop's holding window had closed by `now`, oldest first.

        The window is the crop's own (FR-2508): grain keeps for two weeks while greens last for
        two days, so the comparison is against the sold crop's `escrow_hold_hours` rather than a
        platform-wide figure, and it is readable here as the same arithmetic the domain states -
        inclusive, so an order exactly at the boundary has lapsed.

        Identities rather than rows, because the sweep then locks each one on its own and the
        lock belongs to the settlement rather than to the question of which shipments lapsed. The
        list is bounded so this stays a sweep and not a table scan; a hundred is what a worker's
        first statement may reasonably fetch.
        """
        stmt = (
            select(Order.id)
            .join(Listing, Listing.id == Order.listing_id)
            .join(Crop, Crop.id == Listing.crop_id)
            .where(
                Order.status == OrderStatus.SHIPPED,
                Order.shipped_at + func.make_interval(0, 0, 0, 0, Crop.escrow_hold_hours) <= now,
            )
            .order_by(Order.shipped_at, Order.id)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_lapsed_pickup_ids(self, now: datetime, limit: int) -> list[uuid.UUID]:
        """Pickups whose confirmation window had closed by `now`, oldest first.

        The window is the platform's rather than the crop's: a pickup waits at a place, not on a
        road, so the wait is the fixed day `PICKUP_CONFIRM_WINDOW` states, measured from
        `ready_at`, the moment the seller said the goods were waiting. The comparison is
        inclusive, so a pickup exactly at the boundary has lapsed -- the same answer
        `pickup_window_closed` gives every reader.

        Identities rather than rows, because the sweep then locks each one on its own and the
        lock belongs to the settlement rather than to the question of which pickups lapsed. The
        list is bounded so this stays a sweep and not a table scan.
        """
        # Imported here rather than at module scope: the order service imports this repository,
        # so a module-scope import back into the service would close the cycle. The window and
        # the predicate that judges it must be one value, so this is the constant and not a copy
        # of it.
        from apps.api.services.order_service import PICKUP_CONFIRM_WINDOW

        stmt = (
            select(Order.id)
            .where(
                Order.status == OrderStatus.READY_FOR_PICKUP,
                Order.ready_at + PICKUP_CONFIRM_WINDOW <= now,
            )
            .order_by(Order.ready_at, Order.id)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def crop_hold_hours_for_order(self, order_id: uuid.UUID) -> int | None:
        """The hours an escrow on this order's crop is held before it may be released.

        Read through the order's listing, because the holding window belongs to what was sold
        (FR-2508): grain keeps for two weeks while greens last for two days, and one
        platform-wide window would make the wrong promise to one of them. No crop row is
        `None` on a real order, but a lookup that cannot find one answers `None` rather than
        guessing, and the caller treats "unknown" as "none derived".
        """
        stmt = (
            select(Crop.escrow_hold_hours)
            .select_from(Order)
            .join(Listing, Listing.id == Order.listing_id)
            .join(Crop, Crop.id == Listing.crop_id)
            .where(Order.id == order_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def crop_hold_hours_for_orders(self, order_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Escrow hold hours for several orders at once, keyed by order.

        One statement for the whole batch, because a page of an order list is twenty rows and
        the shipped window is derived for each of them: a lookup per row would be a slow
        request at just the moment the reader asked for their whole order history.
        """
        if not order_ids:
            return {}
        stmt = (
            select(Order.id, Crop.escrow_hold_hours)
            .select_from(Order)
            .join(Listing, Listing.id == Order.listing_id)
            .join(Crop, Crop.id == Listing.crop_id)
            .where(Order.id.in_(order_ids))
        )
        result = await self._session.execute(stmt)
        return dict(result.all())

    async def funded_price_observations(
        self, first_day: date, last_day: date
    ) -> list[tuple[int, str, int, int]]:
        """Every order the platform holds money for, as a price signal for the board.

        One row per order: ``(crop_id, region_code, unit_price_santim, quantity_kg)``. The
        crop and the region are the listing's, because that is where the trade happened — an
        order inherits its geography rather than carrying one of its own.

        An order counts once the platform took money for it, in whatever state it has
        reached since: funded, ready for pickup, shipped, delivered, completed or disputed
        are all one fact as far as the board is concerned — somebody agreed this price for
        real kilos that are moving. Cash on delivery never moves money through the platform,
        so a COD order has no funding moment and never appears here; through a COD-only pilot
        the board rests on what sellers are asking alone, which is the honest state of a
        market nobody has bought from yet.

        The window is measured on the EAT calendar day the order was placed, never on UTC:
        the day a trader in Addis would put the trade in is the day the board's window
        counts (NFR-11), and three hours of drift at either edge would move a trade across a
        boundary no trader would have moved it across.
        """
        stmt = (
            select(
                Listing.crop_id,
                Listing.region_code,
                Order.unit_price_santim,
                Order.quantity_kg,
            )
            .join(Listing, Order.listing_id == Listing.id)
            .where(
                Order.funded_at.is_not(None),
                Order.status.in_(tuple(_FUNDED_STATUSES)),
                func.date_trunc("day", Order.created_at.op("AT TIME ZONE")("Africa/Addis_Ababa"))
                .cast(Date)
                .between(first_day, last_day),
            )
        )
        result = await self._session.execute(stmt)
        return [
            (row["crop_id"], row["region_code"], row["unit_price_santim"], row["quantity_kg"])
            for row in result.mappings().all()
        ]

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        """Discard an unfinished change and release the transaction's locks.

        The worker's per-order transaction is written to be either settled or abandoned, and
        the row lock `SKIP LOCKED` took has to go back the moment the sweep decides not to
        write, or the next candidate waits behind an order nothing is going to happen to.
        """
        await self._session.rollback()

    async def flush(self) -> None:
        await self._session.flush()
