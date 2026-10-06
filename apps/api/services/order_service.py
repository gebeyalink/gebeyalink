"""The rules for turning a listing and a decision into an order.

Everything in this module exists because of one number: what is left of a listing. An order is
created out of stock, and stock is derived from reservations rather than counted on the listing
row, so every question here is a sum, and every move that spends stock happens under a lock on
the listing. That lock is why this module is shaped the way it is — read the listing
`FOR UPDATE`, compute what is left, then decide — and it is what makes two simultaneous sales
of the last kilos produce one order rather than two.

Availability counts reservations in three states and not the fourth. `HELD`, `CONVERTED` and
`CONSUMED` are spoken for; `RELEASED` is how a kilogram becomes sellable again. Delivered weight
never re-enters availability, which is why `CONSUMED` is in the sum and why nothing moves a
reservation out of it.

The status is not a field a request may set. There is exactly one function in this module that
writes an order's status, it consults the state table before it does, and everything that reaches
a status goes through it — including creation, which is born `CREATED` and enters `AWAITING_FUNDING`
in the same transaction that reserves its kilos, because that transition is what makes the
reservation mean something. A COD order is the one exception and it is not a move at all: there is
no money to wait for, so it stays `CREATED`, and the reservation gains its meaning instead when both
parties confirm it (§10.2, FR-2102).

The money is not a field a request may set either. Totals come from the shared money functions
and from the listing, and `fee_payer` is snapshotted onto the row rather than read from
configuration later: an order settled months from now has to be settled under the policy that
was in force when the kilos changed hands.

The funding window is enforced by whoever looks at an order, not by a job. An order waiting for
money past its forty-eight hours is cancelled and its kilos are given back the moment anything
touches it, so a seller is never waiting on a cron that does not exist yet to have their stock
back. That work is a write even when it is reached from a read, and deliberately so: unlike an
offer's window, this one's consequence is another table's contents, and a read that answered
"cancelled" while the kilos were still reserved would be lying in two directions at once.

What is deliberately not here: escrow, payment webhooks, delivery and disputes. Those slices own
their own transitions and will come through the same guard this module uses, which is why the
state table below is the specification's whole table rather than the part this slice happens to
need. Confirmation and COD live here instead: a COD order is the buyer and seller agreeing
between themselves, with no money-moving slice in between for them to wait on.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn
from uuid import UUID

from apps.api.core.idempotency import IdempotencyStore, ReplayedResponse
from apps.api.core.idempotency import fingerprint_of as _fingerprint
from apps.api.core.short_codes import OrderCodeSource, new_pickup_code
from apps.api.models.listing import DeliveryOption, Listing, ListingStatus
from apps.api.models.offer import OfferStatus, proposing_party
from apps.api.models.order import CancelReason, Order, OrderStatus, PaymentMode
from apps.api.models.quantity_reservation import (
    HolderType,
    QuantityReservation,
    ReservationState,
)
from apps.api.repositories.listing_repository import ListingRepository, LoadedListing
from apps.api.repositories.offer_repository import OfferRepository
from apps.api.repositories.order_repository import OrderRepository
from apps.api.repositories.reservation_repository import ReservationRepository
from apps.api.schemas.order import OrderResponse
from apps.api.services.listing_service import effective_status as effective_listing_status
from apps.api.services.listing_service import transition_listing_status
from apps.api.services.offer_service import effective_status as effective_offer_status
from apps.api.services.offer_service import may_answer, transition_offer_status
from packages.shared.errors import ApiError, ErrorCode
from packages.shared.money import FeePayer, OrderTotals, order_totals

logger = logging.getLogger("gebeyalink.api")

#: What a created order is answered with, and what its replay carries. Named here rather than
#: imported from the web framework, because a service that knows about HTTP status codes is a
#: service that has started answering questions about the transport.
CREATED_STATUS = 201
OK_STATUS = 200

#: How long a buyer has to fund an order before it gives its kilos back. Forty-eight hours is the
#: specification's window, and it is a promise to the seller as much as a deadline for the buyer:
#: a seller who took a listing off sale because an order was pending needs to know by when the
#: kilos return.
FUNDING_WINDOW = timedelta(hours=48)

#: How many expired orders one sweep handles. A marketplace whose buyers abandoned a thousand
#: orders overnight should find every one of them, and should find them across several sweeps
#: rather than inside a single request that times out.
EXPIRY_SWEEP_LIMIT = 50

#: The largest goods value a COD order may carry: ten thousand birr, in santim. COD is a credit
#: the seller extends, and a pilot extends it only so far. `subtotal` is the price of the kilos
#: alone — the delivery fee and the platform fee are not part of what the seller is owed for the
#: crop, so they are not part of the ceiling either.
COD_CAP_SANTIM = 1_000_000

#: How long a seller may sit on a confirmed order before the buyer may treat it as a delay.
#: Seventy-two hours, counted from confirmation, and deliberately measured from the moment both
#: parties agreed: before that agreement exists there is nothing to be late on.
SELLER_DELAY_WINDOW = timedelta(hours=72)

#: The whole state table, as which statuses may follow which. Absent from a row means the status
#: is terminal: a completed, cancelled or refunded order is a record of how the trade ended, and
#: none of those is reopened. The rows this slice cannot reach are here anyway — funding,
#: delivery, disputes — because the specification's table is the whole table, and a guard
#: missing an edge is a guard the next slice cannot trust.
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset(
        {
            # An escrow order is born `CREATED` and moves immediately, and a COD order stays
            # `CREATED` until both parties confirm it (FR-2102). Either way, cancellation while
            # it is still `CREATED` is either party's right on a COD order (FR-2103); it is
            # unreachable for escrow only because no escrow order ever sits in `CREATED`.
            OrderStatus.AWAITING_FUNDING,
            OrderStatus.CONFIRMED,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.AWAITING_FUNDING: frozenset({OrderStatus.FUNDED, OrderStatus.CANCELLED}),
    OrderStatus.CONFIRMED: frozenset({OrderStatus.DELIVERED}),
    OrderStatus.FUNDED: frozenset(
        {OrderStatus.READY_FOR_PICKUP, OrderStatus.SHIPPED, OrderStatus.DISPUTED}
    ),
    OrderStatus.READY_FOR_PICKUP: frozenset({OrderStatus.DELIVERED, OrderStatus.DISPUTED}),
    OrderStatus.SHIPPED: frozenset({OrderStatus.DELIVERED, OrderStatus.DISPUTED}),
    OrderStatus.DELIVERED: frozenset({OrderStatus.COMPLETED}),
    # Leaving a dispute has one extra exit, and it is not a status a caller names: rejecting a
    # dispute puts the order back where it was, which is whatever `state_before_dispute` holds.
    # It is added to the allowed set at the point of the transition, out of the row itself.
    OrderStatus.DISPUTED: frozenset({OrderStatus.DELIVERED, OrderStatus.CANCELLED_REFUNDED}),
    OrderStatus.COMPLETED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.CANCELLED_REFUNDED: frozenset(),
}

#: The one status a row may be created in. Kept apart from the table above only because
#: creation has no state to come from, and saying so is more honest than pretending an order is
#: born somewhere and moves.
INITIAL_STATUSES = frozenset({OrderStatus.CREATED})


def lost_to_race(reserved_before: int, reserved_after: int) -> bool:
    """Whether somebody else's sale took stock while this request waited for the listing's lock.

    Both numbers are the same sum of the same rows, taken unlocked immediately before the lock and
    again under it. A larger second one means another transaction committed reservations in
    between, which means the kilos this request was refused for were being sold while it stood in
    line - and that is a different answer from the same sum being no smaller because the seller
    only ever had so much.

    The sweep that runs between the two reads can only make the second number smaller, so it can
    hide a race but never invent one. That is the direction to be wrong in: answering 409 to
    something that was simply unavailable would send a client round a loop that 422 would have
    ended.
    """
    return reserved_after > reserved_before


@dataclass(frozen=True, slots=True)
class OrderTerms:
    """Everything about a sale that is decided rather than asked for, at one moment.

    The address and the delivery method are the buyer's to choose and arrive in the body. The
    weight and the price are the offer's and the listing's, and are read here rather than taken
    from anywhere a caller could reach.
    """

    quantity_kg: int
    unit_price_santim: int
    delivery_method: DeliveryOption
    delivery_fee_santim: int
    buyer_address_woreda_id: int
    buyer_address_text: str

    def money(self, fee_payer: FeePayer) -> OrderTotals:
        """The frozen amounts for this sale, from the shared money functions."""
        return order_totals(
            self.quantity_kg,
            self.unit_price_santim,
            self.delivery_fee_santim,
            fee_payer=fee_payer,
        )


def transition_order_status(
    order: Order, target: OrderStatus, from_status: OrderStatus | None
) -> OrderStatus:
    """Move an order to a new status, if the state table allows it.

    This is the only function in the platform that writes an order's status, and it is called
    by everything that moves one: creation, cancellation, expiry, and the funding, delivery and
    dispute slices that come after. A caller that has not consulted it has not moved an order.

    `from_status` is passed in rather than read off the row, so that callers cannot disagree
    about what the order's current state is. A caller holding a stored row passes
    `effective_status(order, now)` — the status judged against the clock — and creation passes
    `None` to mean "not stored yet": the one move with no state behind it, allowed only to land
    on a status a new order may have.
    """
    if from_status is None:
        allowed = INITIAL_STATUSES
        reported: str | None = None
    else:
        allowed = ALLOWED_TRANSITIONS[from_status]
        # Rejecting a dispute restores the order to the status it held before anybody objected,
        # so that restoration is permitted only to that status and to nothing else on the row.
        if from_status is OrderStatus.DISPUTED and order.state_before_dispute is not None:
            allowed = allowed | {order.state_before_dispute}
        reported = from_status.value

    if target not in allowed:
        logger.warning(
            "refused order transition order_id=%s from=%s to=%s",
            order.id,
            reported if reported is not None else "new",
            target.value,
        )
        raise ApiError(
            ErrorCode.ILLEGAL_STATE_TRANSITION,
            details={
                "order_id": str(order.id),
                "status": reported,
                "requested": target.value,
            },
        )
    order.status = target
    return target


def effective_status(order: Order, now: datetime | None = None) -> OrderStatus:
    """The status an order has when the clock is consulted, rather than when it was written.

    An order still waiting for money past its window is cancelled, whether or not anything has
    got round to writing that down. Deriving it is what stops a listing's kilos from being held
    forever for an order nobody is going to pay for.

    Derived rather than written, because writing belongs to `OrderService._expire_if_stale` and
    only the paths that can afford the write call it. What every reader gets is an honest
    status; what the sale path gets is the kilos back.
    """
    moment = now or datetime.now(UTC)
    if order.status is OrderStatus.AWAITING_FUNDING and funding_window_closed(order, moment):
        return OrderStatus.CANCELLED
    return order.status


def funding_window_closed(order: Order, now: datetime | None = None) -> bool:
    """Whether this order has been waiting for money for longer than it was allowed to.

    Measured from `created_at` because that is when the order entered `AWAITING_FUNDING`: the two
    happen in one transaction and so at one moment, and a column holding the same instant twice
    would be one more thing that could disagree with the row it duplicates.
    """
    moment = now or datetime.now(UTC)
    return order.created_at + FUNDING_WINDOW <= moment


class OrderService:
    """Accepting offers, selling at the asking price, and giving stock back when a deal dies."""

    def __init__(
        self,
        orders: OrderRepository,
        listings: ListingRepository,
        offers: OfferRepository,
        reservations: ReservationRepository,
        *,
        fee_payer: FeePayer,
        cod_enabled: bool,
        codes: OrderCodeSource,
        idempotency: IdempotencyStore,
    ) -> None:
        self._orders = orders
        self._listings = listings
        self._offers = offers
        self._reservations = reservations
        self._fee_payer = fee_payer
        self._cod_enabled = cod_enabled
        self._codes = codes
        self._idempotency = idempotency

    async def accept(
        self, offer_id: UUID, caller_id: UUID, body: dict[str, Any], key: str
    ) -> OrderResponse:
        """The party entitled to answer saying yes, which creates the order and takes the stock.

        One transaction, in this order: the offer and the listing it belongs to, the caller's
        right to answer, the listing's row lock, what is left, the order, the reservation, the
        offer's acceptance, and the sold-out transition if this sale took the last of it. Each of
        those is in the same commit because each is false without the others — an order without a
        reservation is a claim on stock nothing owns, and an accepted offer without an order is a
        negotiation that went nowhere.

        Answering is the other party's move, which is the offer service's rule rather than a
        second one: whoever wrote these terms cannot accept them. So a seller accepts a buyer's
        offer and a buyer accepts the seller's counter, and neither can accept their own.

        Two refusals share that last check and they are told apart. An offer that does not fit
        what is left gets 422 `OFFER_CONFLICT` with the available amount and stays `OPEN`, because
        that is the seller's answer to leave for themselves. An offer that *did* fit and no longer
        does gets 409, because it fitted when this request began and stopped fitting while it was
        waiting for the lock: somebody else's sale took it, and the right instruction is to
        refresh rather than to give up on the bid.

        FR-505's "reject all other open offers" is satisfied by arithmetic rather than by writes,
        per AC-19.1: the other offers stay `OPEN` and their next acceptance fails the same
        availability check. They are logged here so the seller can be told which bids they can no
        longer take.
        """

        async def work() -> Order:
            context = await self._offers.find_in_context(offer_id)
            if context is None:
                raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "offer"})
            if not may_answer(context.offer, context.listing, caller_id):
                raise ApiError(
                    ErrorCode.FORBIDDEN,
                    details={
                        "resource": "offer",
                        "reason": "not_the_answering_party",
                        "proposed_by": proposing_party(context.offer.thread_depth).value,
                    },
                )

            now = datetime.now(UTC)
            # Read unlocked, immediately before the lock, and kept. It is compared with the same
            # sum taken under the lock, and the difference between the two is what tells a refusal
            # somebody else's sale caused from one this seller's own stock caused.
            reserved_before = await self._reserved_before_lock(context.listing)
            # Locked before anything is judged. Availability, the offer's fate and the
            # sold-out transition are all decided from `quantity_kg`, so a second seller
            # arriving at the same moment has to wait here and read what this one left rather
            # than read the same kilos twice.
            listing = await self._lock_listing(context.listing.id)
            # Stock held by an order whose funding window has closed is given back before
            # anything is counted, so a lapsed hold can never be the reason a live bid fails.
            await self._expire_stale_on_listing(listing.id, now)
            available, reserved_after = await self._availability(listing)
            self._require_sellable(
                listing,
                now,
                requested_kg=context.offer.quantity_kg,
                available=available,
                lost=lost_to_race(reserved_before, reserved_after),
            )

            # Read again now that the lock is held. The first read happened before it, so it
            # described the negotiation as it stood before anybody accepted anything on this
            # listing: a bid accepted a moment ago would still look `OPEN` here, and two sellers
            # accepting that bid at once would both be told yes and both would create an order.
            # Offering the buyer here rather than a not-found is deliberate — the buyer is a
            # column, and nothing in this slice deletes a bid.
            offer = await self._offers.refresh(offer_id) or context.offer

            if offer.quantity_kg > available:
                self._refuse(
                    listing,
                    requested_kg=offer.quantity_kg,
                    available=available,
                    lost_to_race=lost_to_race(reserved_before, reserved_after),
                    refusal=ApiError(
                        ErrorCode.OFFER_CONFLICT,
                        details={
                            "listing_id": str(listing.id),
                            "offer_id": str(offer.id),
                            "requested_kg": offer.quantity_kg,
                            "available": available,
                        },
                    ),
                )

            # Reaches the guard with the offer's clock-judged status, so an offer whose window
            # closed while nobody was looking cannot be accepted.
            transition_offer_status(offer, OfferStatus.ACCEPTED, effective_offer_status(offer, now))

            terms = self._terms(
                listing,
                body,
                quantity_kg=offer.quantity_kg,
                unit_price_santim=offer.price_santim,
            )
            order = await self._open_order(
                listing, terms, buyer_id=offer.buyer_id, offer_id=offer.id
            )
            await self._note_unfulfillable_offers(listing, available - offer.quantity_kg)
            return order

        return await self._once(
            caller_id, key, ("accept", str(offer_id)), body, CREATED_STATUS, work
        )

    async def buy_now(
        self, listing_id: UUID, caller_id: UUID, body: dict[str, Any], key: str
    ) -> OrderResponse:
        """The buyer taking the asking price, which creates the order and takes the stock.

        The same transaction as an acceptance, minus the offer: the weight is the buyer's choice
        and the price is the listing's, so there is nothing to negotiate and nothing to agree.
        All three bounds are checked before the arithmetic — the listing's minimum order, because a
        seller who will not sell a kilo is not obliged to sell half of one; availability, because
        the seller may have sold the rest of it an hour ago; and the same distinction between a
        request that never fitted and one that lost a race for the last kilos.

        COD is a choice in the body, and it is a choice with two refusals of its own, both 422. A
        mode the platform has not switched on is refused by name — asking for COD where the flag is
        off is a request for something that does not exist — and an order worth more than the
        ceiling a pilot will extend is refused as over the limit, with the ceiling in the reply.
        """

        async def work() -> Order:
            now = datetime.now(UTC)
            # Read unlocked first, for two reasons: a listing that is not there should be a 404
            # without taking a lock anybody else is waiting on, and the weight is parsed before
            # anything is judged rather than after.
            fresh = await self._require(listing_id)
            quantity_kg = int(body["quantity_kg"])
            reserved_before = await self._reserved_before_lock(fresh.listing)
            listing = await self._lock_listing(listing_id)
            await self._expire_stale_on_listing(listing.id, now)
            available, reserved_after = await self._availability(listing)
            self._require_sellable(
                listing,
                now,
                requested_kg=quantity_kg,
                available=available,
                lost=lost_to_race(reserved_before, reserved_after),
            )

            if listing.seller_id == caller_id:
                raise ApiError(
                    ErrorCode.VALIDATION_ERROR,
                    details={"fields": {"listing_id": "you_cannot_buy_your_own_listing"}},
                )

            if quantity_kg < listing.min_order_kg:
                raise ApiError(
                    ErrorCode.VALIDATION_ERROR,
                    details={
                        "fields": {"quantity_kg": "must_be_at_least_listing_min_order_kg"},
                        "min_order_kg": listing.min_order_kg,
                    },
                )

            if quantity_kg > available:
                self._refuse(
                    listing,
                    requested_kg=quantity_kg,
                    available=available,
                    lost_to_race=lost_to_race(reserved_before, reserved_after),
                    refusal=ApiError(
                        ErrorCode.VALIDATION_ERROR,
                        details={
                            "fields": {"quantity_kg": "exceeds_available_quantity"},
                            "available": available,
                        },
                    ),
                )

            terms = self._terms(
                listing,
                body,
                quantity_kg=quantity_kg,
                unit_price_santim=listing.unit_price_santim,
            )
            payment_mode = PaymentMode(body.get("payment_mode", PaymentMode.ESCROW.value))
            if payment_mode is PaymentMode.COD:
                if not self._cod_enabled:
                    raise ApiError(
                        ErrorCode.VALIDATION_ERROR,
                        details={"feature": "cod"},
                    )
                subtotal = terms.money(self._fee_payer).subtotal
                if subtotal > COD_CAP_SANTIM:
                    raise ApiError(
                        ErrorCode.LIMIT_EXCEEDED,
                        details={
                            "limit_santim": COD_CAP_SANTIM,
                            "subtotal_santim": subtotal,
                        },
                    )
            return await self._open_order(
                listing, terms, buyer_id=caller_id, offer_id=None, payment_mode=payment_mode
            )

        return await self._once(
            caller_id, key, ("buy-now", str(listing_id)), body, CREATED_STATUS, work
        )

    async def read(
        self, order_id: UUID, caller_id: UUID, now: datetime | None = None
    ) -> OrderResponse:
        """One order, for the two parties to it and nobody else.

        Existence is answered before authorization on purpose: a caller who may not touch a row
        still needs to know whether it is there, because the short code in a text message is the
        only route anybody has to it.

        This read is also where the funding window is enforced. Touching an order that has been
        waiting too long gives its kilos back on the spot, which is the lazy evaluation the
        specification asks for in place of the cron that does not exist yet.
        """
        moment = now or datetime.now(UTC)
        order = await self._orders.find_by_id(order_id)
        if order is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "order"})
        self._require_party(order, caller_id)
        await self._expire_if_stale(order, moment)
        return to_response(order, now=moment)

    async def read_mine(
        self,
        caller_id: UUID,
        *,
        buyer: bool,
        status_filter: OrderStatus | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[OrderResponse], str | None, bool]:
        """One page of the orders the caller is on one side of, newest first.

        Which side is the caller's to say, and the two sides are read from different columns, so
        saying so is the whole of the authorization here: an order the caller is not a party to
        is not in either list.

        Deliberately read-only, unlike `read`. A list is a page of twenty orders, and making a
        page of history a write would put a transaction behind a scroll. The status each row
        carries is still judged against the clock, so an expired order reads as cancelled; what
        this path does not do is hand the kilos back, and the paths that could lose a sale to a
        stale hold — the acceptance and buy-now flows — sweep for themselves before they count.
        """
        after = decode_cursor(cursor) if cursor else None
        page = await self._orders.page_for_party(
            caller_id, buyer=buyer, status=status_filter, after=after, limit=limit
        )
        next_cursor = encode_cursor(page.orders[-1]) if page.has_more and page.orders else None
        return [to_response(order) for order in page.orders], next_cursor, page.has_more

    async def confirm(self, order_id: UUID, caller_id: UUID) -> OrderResponse:
        """A party to a COD order recording that the terms are the ones they agreed to.

        Confirmation is the COD stand-in for funding: money never moves, and instead each
        party signs the order. It is idempotent per party, which is why there is no
        `Idempotency-Key` on this route — the party *is* the key. A buyer who confirms twice is
        told once, because a confirmation that ran twice would be no different from one that ran
        once, and there is no stock to double-release for a mistake to be made in.

        The order reaches `CONFIRMED` on the second distinct confirmation, whichever party is
        second (the two may confirm in either order), and it is that transition which converts
        the reservation from `HELD` to `CONVERTED` — the identical step the funding and delivery
        slices use when escrow money arrives, so a confirmed COD order blocks stock exactly the
        way a funded one does. `confirmed_at` is set at that same instant, and it is what the
        seller-delayed window is measured from.

        Every other status is refused by the guard: an escrow order is `AWAITING_FUNDING` and is
        confirmed by its money, not by a party's signature, so this slice adds no move to it at
        all. A cancelled order is a record and is not reopened.
        """

        order = await self._orders.find_by_id_for_update(order_id)
        if order is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "order"})
        self._require_party(order, caller_id)

        now = datetime.now(UTC)
        current = effective_status(order, now)
        if current not in (OrderStatus.CREATED, OrderStatus.CONFIRMED):
            # Not a COD order still waiting to be confirmed, nor one already confirmed. The
            # guard raises the 409 naming the state, and it is the same refusal an escrow
            # order gets from every move this slice does not own.
            transition_order_status(order, OrderStatus.CONFIRMED, current)
        if caller_id == order.buyer_id:
            if order.buyer_confirmed_at is None:
                order.buyer_confirmed_at = now
        elif order.seller_confirmed_at is None:
            order.seller_confirmed_at = now

        if (
            order.buyer_confirmed_at is not None
            and order.seller_confirmed_at is not None
            and current is OrderStatus.CREATED
        ):
            transition_order_status(order, OrderStatus.CONFIRMED, current)
            order.confirmed_at = now
            converted = await self._reservations.convert_for_order(order.id)
            logger.info(
                "confirmed order order_id=%s converted_kg=%s",
                order.id,
                converted,
            )
        await self._orders.commit()
        return to_response(order, now=now)

    async def cancel(self, order_id: UUID, caller_id: UUID, key: str) -> OrderResponse:
        """A party walking away from an order that has not been paid for or confirmed.

        Who may walk, and when, is decided by the mode. An escrow order is the buyer's alone to
        cancel and only while it is still waiting for money: FR-607 gives the seller a
        cancellation too, but only before acceptance — and every escrow order this slice can see
        is an acceptance, so the seller's answer is that they cannot, and it is 403 rather than
        409 because the seller has not asked for something that does not exist yet. A COD order
        is different: nobody has paid and nobody expects to, so while it is still `CREATED` —
        before both parties have confirmed — either party may cancel it (FR-2103). Once a COD
        order is confirmed, nobody walks away: both parties signed, and undoing that is a
        support decision, which is a 409 from the guard rather than a route.

        The kilos go back in the same transaction as the cancellation. An order cancelled with
        its reservation still held would be stock nobody can buy and nobody can count, and the
        listing's availability would be quietly wrong for as long as the row survived.
        """

        async def work() -> Order:
            order = await self._orders.find_by_id_for_update(order_id)
            if order is None:
                raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "order"})
            # Locked, because "read it, check it can move, move it" is three steps and two
            # concurrent cancellations would otherwise both read the same status and both
            # believe they were the one to give the kilos back.
            self._require_party(order, caller_id)

            now = datetime.now(UTC)
            current = effective_status(order, now)
            if current is OrderStatus.CREATED:
                # A COD order neither party has confirmed: whoever walks away names the reason
                # (FR-2103). `OTHER` is the seller's word — there is no `SELLER_CANCELLED` in the
                # vocabulary the API contract gives, and `BUYER_CANCELLED` would be a lie about
                # who cancelled.
                order.cancel_reason = (
                    CancelReason.OTHER.value
                    if caller_id == order.seller_id
                    else CancelReason.BUYER_CANCELLED.value
                )
            else:
                if order.payment_mode is PaymentMode.COD:
                    # Confirmed, or past it: the state table gives `CANCELLED` no edge from
                    # `CONFIRMED`, so the guard's 409 is the whole answer. Both parties get it;
                    # there is no seller-specific refusal to add on top of a state that refuses
                    # everybody.
                    transition_order_status(order, OrderStatus.CANCELLED, current)
                if caller_id == order.seller_id:
                    raise ApiError(
                        ErrorCode.FORBIDDEN,
                        details={
                            "resource": "order",
                            "reason": "seller_cannot_cancel_after_acceptance",
                        },
                    )
                order.cancel_reason = CancelReason.BUYER_CANCELLED.value
            transition_order_status(order, OrderStatus.CANCELLED, current)
            released = await self._reservations.release_for_order(order.id)
            logger.info("cancelled order order_id=%s released_kg=%s", order.id, released)
            await self._orders.commit()
            return order

        return await self._once(caller_id, key, ("cancel", str(order_id)), {}, OK_STATUS, work)

    async def _once(
        self,
        caller_id: UUID,
        key: str,
        what: tuple[str, ...],
        body: dict[str, Any],
        status_code: int,
        work: Callable[[], Awaitable[Order]],
    ) -> OrderResponse:
        """Do this mutation once, whatever the client does about sending it again.

        The claim is taken before the work starts and the answer is stored after it commits, so a
        second request under the same key finds the first one's answer rather than a second
        order. A failure hands the key straight back: caching a validation failure would make a
        typo permanent for a day, which is not something idempotency is for.
        """
        guard = await self._idempotency.claim(str(caller_id), key, _fingerprint(*what, body))
        if isinstance(guard, ReplayedResponse):
            logger.info("replayed idempotent response status=%s", guard.status_code)
            return OrderResponse.model_validate(guard.body)
        try:
            order = await work()
        except BaseException:
            await guard.release()
            raise
        response = to_response(order)
        await guard.record(status_code, response.model_dump(mode="json"))
        return response

    async def _lock_listing(self, listing_id: UUID) -> Listing:
        """The listing, with its row locked until this transaction ends, or a 404."""
        loaded = await self._listings.find_by_id_for_update(listing_id)
        if loaded is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "listing"})
        return loaded.listing

    async def _require(self, listing_id: UUID) -> LoadedListing:
        """The listing, read without a lock, or a 404.

        A refusal that needs no lock should not take one: a request for a listing that does not
        exist should not stand in front of a seller who is trying to sell stock.
        """
        loaded = await self._listings.find_by_id(listing_id)
        if loaded is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "listing"})
        return loaded

    async def _availability(self, listing: Listing) -> tuple[int, int]:
        """What is left of this listing, and how much of it was already spoken for.

        Handed back as a pair because the two questions are always asked together: how much a
        caller may have, and whether that figure changed while they waited. One sum over `HELD`,
        `CONVERTED` and `CONSUMED` answers both; `RELEASED` is not in it, which is how a
        cancelled or expired order's kilos become sellable again.
        """
        reserved = await self._reservations.reserved_kg_for_listing(listing.id)
        return listing.quantity_kg - reserved, reserved

    async def _reserved_before_lock(self, listing: Listing) -> int:
        """How many kilos were already spoken for before this request began waiting for the lock.

        Read unlocked, once, immediately before the lock is taken. Its only purpose is the
        comparison in `lost_to_race`, and it is worth exactly one aggregate query.
        """
        return await self._reservations.reserved_kg_for_listing(listing.id)

    def _refuse(
        self,
        listing: Listing,
        *,
        requested_kg: int,
        available: int,
        lost_to_race: bool,
        refusal: ApiError,
    ) -> NoReturn:
        """Answer a sale that cannot happen: 409 if it lost a race, otherwise `refusal`.

        The distinction is FR-1903's, and it is the difference between two things a client must
        treat differently. `refusal` means these terms were never possible against this seller's
        stock, and no retry will change that. 409 means somebody else committed while this request
        was queued behind the lock: the stock was there, the answer may be different now, and the
        client is told to look again rather than to give up a bid that was affordable a moment ago.

        Named rather than inlined at each refusal because the two sale paths have three refusals
        between them and they must not drift into answering differently.
        """
        if not lost_to_race:
            raise refusal
        logger.warning(
            "lost a race for the last kilos listing_id=%s requested_kg=%s available_kg=%s"
            " status=%s",
            listing.id,
            requested_kg,
            available,
            effective_listing_status(listing).value,
        )
        raise ApiError(
            ErrorCode.CONCURRENT_MODIFICATION,
            details={
                "listing_id": str(listing.id),
                "requested_kg": requested_kg,
                "available": available,
                "status": effective_listing_status(listing).value,
            },
        )

    def _require_sellable(
        self, listing: Listing, now: datetime, *, requested_kg: int, available: int, lost: bool
    ) -> None:
        """Refuse a sale of a listing that is not currently on sale.

        Read through the listing service's own rule for what a listing's status is, so a lapsed
        listing is as unavailable to a sale as it is to a search: one definition of "live", not
        two that can drift.

        A paused listing refuses even an acceptance, which is the seller's own statement that the
        listing is not for sale. The offer stays `OPEN`, so a seller who pauses by accident
        resumes and answers it, and the buyer's bid has not been spent.

        A listing that is sold out because another sale emptied it while this request waited is the
        one refusal here that is a race rather than a rule, and it is answered as one: the seller
        did not take the listing off sale, and 409 says so.
        """
        status = effective_listing_status(listing, now=now)
        if status is ListingStatus.ACTIVE:
            return
        logger.warning("refused sale listing_id=%s status=%s", listing.id, status.value)
        self._refuse(
            listing,
            requested_kg=requested_kg,
            available=available,
            lost_to_race=lost,
            refusal=ApiError(
                ErrorCode.LISTING_NOT_ACTIVE,
                details={"listing_id": str(listing.id), "status": status.value},
            ),
        )

    def _require_party(self, order: Order, caller_id: UUID) -> None:
        """Refuse a caller who is neither the buyer nor the seller of this order."""
        if caller_id not in (order.buyer_id, order.seller_id):
            raise ApiError(
                ErrorCode.FORBIDDEN,
                details={"resource": "order", "reason": "not_a_party_to_this_order"},
            )

    def _terms(
        self,
        listing: Listing,
        body: dict[str, Any],
        *,
        quantity_kg: int,
        unit_price_santim: int,
    ) -> OrderTerms:
        """The terms of this sale, with the parts the buyer chose checked against the listing.

        The delivery method has to be one the listing offers, and the delivery fee is the
        listing's own: a method the listing does not carry is a journey nobody agreed to pay
        for, and charging the seller's delivery fee for a buyer who arranged their own cart would
        be inventing a charge the listing never offered.
        """
        method = DeliveryOption(body["delivery_method"])
        if method not in tuple(listing.delivery_options):
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={"fields": {"delivery_method": "not_offered_by_this_listing"}},
            )
        return OrderTerms(
            quantity_kg=quantity_kg,
            unit_price_santim=unit_price_santim,
            delivery_method=method,
            delivery_fee_santim=(
                listing.delivery_fee_santim if method is DeliveryOption.SELLER_DELIVERY else 0
            ),
            buyer_address_woreda_id=int(body["buyer_address_woreda_id"]),
            buyer_address_text=str(body["buyer_address_text"]),
        )

    async def _open_order(
        self,
        listing: Listing,
        terms: OrderTerms,
        *,
        buyer_id: UUID,
        offer_id: UUID | None,
        payment_mode: PaymentMode = PaymentMode.ESCROW,
    ) -> Order:
        """Write the order, reserve its kilos, mark the listing sold out if it was the last of it.

        Three rows, one commit. The reservation names the order and the order names the
        listing, so either alone is incomplete, and the sold-out transition is written here
        rather than left to the cron because a listing that has just sold its last kilo and still
        reads as active would take another bid it cannot honour.

        The born transition depends on the mode. An escrow order enters `AWAITING_FUNDING` in
        the same transaction that reserves its kilos, because that is what its hold means. A COD
        order stays `CREATED` — there is no money to wait for, so `AWAITING_FUNDING` would be a
        lie about the trade — and its hold means something only once both parties confirm it.
        """
        totals = terms.money(self._fee_payer)
        order = Order(
            short_code=await self._codes.draw(),
            buyer_id=buyer_id,
            # The seller is the listing's, recorded here so that who owed whom on the day is a
            # fact about the order rather than a lookup that could answer differently later.
            seller_id=listing.seller_id,
            listing_id=listing.id,
            offer_id=offer_id,
            quantity_kg=terms.quantity_kg,
            unit_price_santim=terms.unit_price_santim,
            delivery_method=terms.delivery_method,
            delivery_fee_santim=terms.delivery_fee_santim,
            platform_fee_santim=totals.platform_fee,
            vat_santim=totals.vat,
            total_santim=totals.buyer_total,
            # Snapshotted, not read again at settlement: a policy change is not a change to
            # what the two parties agreed.
            fee_payer=self._fee_payer,
            buyer_address_woreda_id=terms.buyer_address_woreda_id,
            buyer_address_text=terms.buyer_address_text,
            pickup_code=new_pickup_code(),
            status=OrderStatus.CREATED,
            payment_mode=payment_mode,
            # Version one, as on a listing and an offer: version counts the changes a row has
            # been through, and an order nobody has touched has been through being created.
            version=1,
        )
        if payment_mode is not PaymentMode.COD:
            # Born `CREATED` and moved like everything else, so the transition that gives an
            # escrow order's reservation its meaning has been through the guard too. A COD order
            # stays where it was born: its reservation gains meaning at confirmation (§10.2).
            transition_order_status(order, OrderStatus.AWAITING_FUNDING, OrderStatus.CREATED)
        await self._orders.add(order)
        await self._reservations.add(
            QuantityReservation(
                listing_id=listing.id,
                holder_type=HolderType.ORDER,
                holder_id=order.id,
                quantity_kg=terms.quantity_kg,
                state=ReservationState.HELD,
            )
        )

        # Read after the reservation is written, so it counts this order's own kilos: availability
        # is the sum, and the sum already includes the row added above. Subtracting this order's
        # weight again would be the classic off-by-one-order, and it would sell out a listing
        # that still had stock in it.
        remaining, _ = await self._availability(listing)
        if remaining <= 0:
            # `ACTIVE → SOLD_OUT`, which the state table allows only from `ACTIVE` and which
            # `_require_sellable` has just established. Released stock does not bring a listing
            # back: §10.1 gives `SOLD_OUT` no edge to `ACTIVE`, so a cancellation on a sold-out
            # listing leaves it sold out and the seller closes it or lists again.
            transition_listing_status(listing, ListingStatus.SOLD_OUT)

        await self._orders.commit()
        return order

    async def _note_unfulfillable_offers(self, listing: Listing, remaining_kg: int) -> None:
        """Say which of the seller's other open bids this sale has made impossible.

        Nothing is written to them. FR-505 asks that offers too large for what is left be
        rejected, and AC-19.1 asks that they stay `OPEN`, so the rejection is the availability
        check these offers will now fail — and a log line is how the seller finds out, because
        an offer that cannot be accepted without anybody saying so is a bid left hanging on a
        listing with nothing behind it.

        Asked for after the sale, when what is left is known, and asked for from every open offer
        rather than from the offers that are too large: the interesting number is the first one,
        because it is the smallest that can no longer be accepted.
        """
        stranded = [
            offer
            for offer in await self._offers.list_for_listing(listing.id)
            if offer.status is OfferStatus.OPEN and offer.quantity_kg > remaining_kg
        ]
        for offer in stranded:
            logger.info(
                "open offer can no longer be accepted offer_id=%s listing_id=%s remaining_kg=%s",
                offer.id,
                listing.id,
                remaining_kg,
            )

    async def _expire_stale_on_listing(self, listing_id: UUID, now: datetime) -> int:
        """Cancel every order on this listing whose funding window closed, and say how many.

        Run before availability is counted, and that is the order that matters: a hold nobody is
        going to pay for must not be able to make a live bid fail. Bounded, because a listing
        with a thousand lapsed orders should not turn a sale into a sweep, and the next sale will
        finish the rest.
        """
        stale = await self._orders.list_unfunded_past_on_listing(
            listing_id, cutoff=now - FUNDING_WINDOW, limit=EXPIRY_SWEEP_LIMIT
        )
        expired = 0
        for order in stale:
            transition_order_status(order, OrderStatus.CANCELLED, OrderStatus.AWAITING_FUNDING)
            order.cancel_reason = CancelReason.FUNDING_EXPIRED.value
            await self._reservations.release_for_order(order.id)
            expired += 1
        if expired:
            logger.info("expired %s stale order(s) on listing_id=%s", expired, listing_id)
        return expired

    async def _expire_if_stale(self, order: Order, now: datetime) -> bool:
        """Cancel this one order if its window closed, and give its kilos back. Did it, or not.

        Committed on its own rather than left to the caller, because the caller is a read and
        there is nothing else in its transaction to commit with. The alternative — leaving the
        transaction open for a caller that does not write — would hold the row lock for as long
        as the response took to serialize.
        """
        if order.status is not OrderStatus.AWAITING_FUNDING or not funding_window_closed(
            order, now
        ):
            return False
        transition_order_status(order, OrderStatus.CANCELLED, OrderStatus.AWAITING_FUNDING)
        order.cancel_reason = CancelReason.FUNDING_EXPIRED.value
        released = await self._reservations.release_for_order(order.id)
        logger.info("expired order order_id=%s released_kg=%s", order.id, released)
        await self._orders.commit()
        return True


def to_response(order: Order, now: datetime | None = None) -> OrderResponse:
    """Read an order out for a caller, with its status judged against the clock.

    The money comes out as it was frozen, never recomputed: an order's total is what two parties
    agreed to, and a figure that changed because a configuration value moved would be a
    different agreement.

    `seller_delayed` is derived exactly the way `effective_status` derives a lapsed funding
    window — a lie on read would be a lie on the thing a buyer acts on. An order that has never
    been confirmed is never delayed, because the window only starts once there is an agreement
    to be late on.
    """
    moment = now or datetime.now(UTC)
    confirmed = order.confirmed_at
    return OrderResponse(
        id=order.id,
        short_code=order.short_code,
        buyer_id=order.buyer_id,
        seller_id=order.seller_id,
        listing_id=order.listing_id,
        offer_id=order.offer_id,
        quantity_kg=order.quantity_kg,
        unit_price_santim=order.unit_price_santim,
        delivery_method=order.delivery_method,
        delivery_fee_santim=order.delivery_fee_santim,
        platform_fee_santim=order.platform_fee_santim,
        vat_santim=order.vat_santim,
        total_santim=order.total_santim,
        buyer_address_woreda_id=order.buyer_address_woreda_id,
        buyer_address_text=order.buyer_address_text,
        pickup_code=order.pickup_code,
        status=effective_status(order, moment),
        state_before_dispute=order.state_before_dispute,
        funded_at=order.funded_at,
        shipped_at=order.shipped_at,
        delivered_at=order.delivered_at,
        delivered_weight_kg=order.delivered_weight_kg,
        cancel_reason=order.cancel_reason,
        payment_mode=order.payment_mode,
        confirmed_at=order.confirmed_at,
        seller_delayed=(confirmed is not None and confirmed + SELLER_DELAY_WINDOW < moment),
        fee_payer=order.fee_payer,
        version=order.version,
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


def encode_cursor(order: Order) -> str:
    """Where a page ended, in a form the caller carries without reading.

    Both the time and the identity are inside it, because ordering by time alone is not an order
    when two orders share a time, which most of them do: a listing's whole stock can be sold in
    one transaction, and then all of those rows have the same timestamp.
    """
    raw = f"{order.created_at.isoformat()}|{order.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Read a page boundary back, refusing one that is not one we wrote."""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        stamp, _, identity = raw.partition("|")
        return datetime.fromisoformat(stamp), UUID(identity)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, details={"fields": {"cursor": "not_a_valid_cursor"}}
        ) from exc
