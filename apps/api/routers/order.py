"""Eleven routes: two ways to create an order, a confirmation, a cancellation, five
fulfillment moves, and two reads.

An order is created in exactly two ways and both of them are here, because they are the same
transaction with one row missing: accepting an offer that already agreed a price and weight, and
buying now at the asking price. The money is not in the body of either, and `extra="forbid"` on
the schemas is what makes that true rather than aspirational — a client sending `total_santim`
is refused by name instead of being quietly ignored.

Acceptance lives on the offer rather than on the order, and buy-now on the listing rather than
on the order, because that is where the decision was made. FR-608's buy-now path is
`/listings/:id/buy-now` and an acceptance belongs to the offer's own history; an order is the
consequence of both and gets no route of its own for something it did not decide.

Cancellation takes no body, and confirmation takes no body either. The reason is
`BUYER_CANCELLED` because a buyer cancelling and a funding window closing are different facts
about the future, and one word for both would lose that; a body that could carry a reason would
be a body whose contents nothing acts on. Confirmation is the COD stand-in for funding — each
party signs instead of money moving — and a party's identity is the idempotency key, so it is
the one mutation that does not ask for the header.

Fulfillment is where the crop itself changes hands, and it is five routes because it is five
facts with different authors. The seller marks a pickup ready (FR-701), marks a delivery shipped
(FR-702), breaks the buyer's pickup code at the handover (FR-703), and records what the scale
said, separately from either confirmation because a scale reading can be a fact long before
anybody signs anything (FR-2507). The buyer confirms the shipped delivery arrived within the
recorded weight's tolerance (FR-702), the one message the money never moves around.

The reads answer the same question from two directions — "what is happening to my crops" — and
the five fulfillment moves require `Idempotency-Key` because each of them could do its work
twice: a second settled delivery is stock consumed twice, and a second recorded weight is a
dispute reading the later number against the earlier handover. A confirmation cannot double
anything — the second tap by the same party is a no-op by construction — so it does not ask.

Both reads are scoped to the caller's own party on an order, because an order is the only row in
the platform that names two accounts. It is also where a buyer's address and a seller's committed
stock meet, so a third party reading it would be a data leak rather than a privacy preference.
`?as=` is the caller's to state and the two values read different columns, which is the whole of
the authorization: an order the caller is not a party to appears in neither list.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, status

from apps.api.dependencies import AccessTokenDep, OrderServiceDep
from apps.api.models.order import OrderStatus
from apps.api.schemas.order import (
    OrderAcceptBody,
    OrderBuyNowBody,
    OrderResponse,
    PickupConfirmBody,
    WeightBody,
)
from apps.api.schemas.pagination import CursorPage

router = APIRouter(tags=["Orders"])

#: The page size ceiling from the pagination convention. Beyond it a single response carries
#: more of a trader's history than a phone on a slow connection should be asked to render.
MAX_PAGE_SIZE = 100

#: A UUID, because that is what the API contract says this header is, and a client that invents
#: its own format for it is a client whose retry will not match somebody else's. Declared as a
#: UUID rather than as a string of some length, so a malformed key is refused by the framework
#: before any business rule is consulted: a key nobody can read is a key that cannot be stored,
#: and storing it under a mangled name would be worse than refusing it.
IdempotencyKey = Annotated[
    UUID,
    Header(
        alias="Idempotency-Key",
        description=(
            "Identifies this intent. Send the same key again to get the same answer without "
            "the same work being done twice."
        ),
    ),
]

OrderIdPath = Path(description="The order's internal identity, not its short code.")
OfferIdPath = Path(description="The offer's internal identity.")
ListingIdPath = Path(description="The listing's internal identity, not its short code.")


@router.post(
    "/offers/{offer_id}/accept",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def accept_offer(
    body: OrderAcceptBody,
    caller: AccessTokenDep,
    orders: OrderServiceDep,
    idempotency_key: IdempotencyKey,
    offer_id: UUID = OfferIdPath,
) -> OrderResponse:
    """The seller saying yes: the offer becomes an order and its kilos become spoken for.

    One transaction, and a refusal when there is no longer enough to sell leaves the offer exactly
    where it was. A bid that no longer fits is not a bid the platform should retire on the
    seller's behalf — the seller is the one who has to look at their other buyers.
    """
    return await orders.accept(
        offer_id, caller.user_id, body.model_dump(mode="json"), str(idempotency_key)
    )


@router.post(
    "/listings/{listing_id}/buy-now",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def buy_now(
    body: OrderBuyNowBody,
    caller: AccessTokenDep,
    orders: OrderServiceDep,
    idempotency_key: IdempotencyKey,
    listing_id: UUID = ListingIdPath,
) -> OrderResponse:
    """The buyer taking the asking price at the listing's own terms.

    The weight is the buyer's to choose and the price is not the buyer's to change: it is the
    listing's, which is the entire difference between buying and bidding.
    """
    return await orders.buy_now(
        listing_id, caller.user_id, body.model_dump(mode="json"), str(idempotency_key)
    )


@router.post("/orders/{order_id}/cancel", response_model=OrderResponse)
async def cancel_order(
    caller: AccessTokenDep,
    orders: OrderServiceDep,
    idempotency_key: IdempotencyKey,
    order_id: UUID = OrderIdPath,
) -> OrderResponse:
    """A party walking away from an order that has not been paid for or confirmed.

    Who may walk depends on the mode: an escrow order's buyer while it is still waiting for
    money, or either party to a COD order while it is still `CREATED`. The seller cannot cancel
    an escrow order — an order is what acceptance creates, and FR-607 gives a seller their way
    to stop a bid before it becomes one, which is rejecting it.
    """
    return await orders.cancel(order_id, caller.user_id, str(idempotency_key))


@router.post("/orders/{order_id}/confirm", response_model=OrderResponse)
async def confirm_order(
    caller: AccessTokenDep,
    orders: OrderServiceDep,
    order_id: UUID = OrderIdPath,
) -> OrderResponse:
    """A party to a COD order agreeing that the terms are the ones they signed up for.

    Money never moves on a COD order, so confirmation is the funding step and the route is the
    one mutation that needs no `Idempotency-Key`: the party *is* the key, and their second
    confirmation is a no-op by construction. The order reaches `CONFIRMED` once both parties
    have confirmed, in either order.
    """
    return await orders.confirm(order_id, caller.user_id)


@router.post("/orders/{order_id}/ready", response_model=OrderResponse)
async def mark_ready(
    caller: AccessTokenDep,
    orders: OrderServiceDep,
    idempotency_key: IdempotencyKey,
    order_id: UUID = OrderIdPath,
) -> OrderResponse:
    """The seller's word that a pickup order is waiting to be collected (FR-701).

    The readiness stands alone because the move it makes is not a delivery: the goods are
    standing by, not handed over, and the handover ends here with the seller's pickup
    confirmation rather than with this message.
    """
    return await orders.mark_ready(order_id, caller.user_id, str(idempotency_key))


@router.post("/orders/{order_id}/shipped", response_model=OrderResponse)
async def mark_shipped(
    caller: AccessTokenDep,
    orders: OrderServiceDep,
    idempotency_key: IdempotencyKey,
    order_id: UUID = OrderIdPath,
) -> OrderResponse:
    """The seller's word that a delivery order is on its way (FR-702).

    Measured from this message the crop's own holding window starts, because the kilos are only
    on the move from the moment they are sent.
    """
    return await orders.mark_shipped(order_id, caller.user_id, str(idempotency_key))


@router.post("/orders/{order_id}/confirm-pickup", response_model=OrderResponse)
async def confirm_pickup(
    body: PickupConfirmBody,
    caller: AccessTokenDep,
    orders: OrderServiceDep,
    idempotency_key: IdempotencyKey,
    order_id: UUID = OrderIdPath,
) -> OrderResponse:
    """The seller breaking the buyer's pickup code, and with it the handover counting as done
    (FR-703).

    The code is the proof that the person presenting it holds the buyer's side of the order —
    which is why the seller's copy of an order never carries it.
    """
    return await orders.confirm_pickup(
        order_id, caller.user_id, body.model_dump(mode="json"), str(idempotency_key)
    )


@router.post("/orders/{order_id}/confirm-delivery", response_model=OrderResponse)
async def confirm_delivery(
    caller: AccessTokenDep,
    orders: OrderServiceDep,
    idempotency_key: IdempotencyKey,
    order_id: UUID = OrderIdPath,
) -> OrderResponse:
    """The buyer's word that a shipped order arrived, and was received (FR-702).

    The buyer's confirmation is the delivery's other boundary: a shortfall past the configured
    tolerance is refused here, because it is the buyer who carries the risk of it.
    """
    return await orders.confirm_delivery(order_id, caller.user_id, str(idempotency_key))


@router.put("/orders/{order_id}/weight", response_model=OrderResponse)
async def record_weight(
    body: WeightBody,
    caller: AccessTokenDep,
    orders: OrderServiceDep,
    idempotency_key: IdempotencyKey,
    order_id: UUID = OrderIdPath,
) -> OrderResponse:
    """Record what the scale said against an order in transit (FR-2507).

    Recording is separate from confirming: the weight is a fact that belongs to the handover,
    and the tolerance comes in when it is weighed against what was ordered — at the buyer's
    confirmation — so the reading may be recorded before anybody signs anything.
    """
    return await orders.record_weight(
        order_id, caller.user_id, body.model_dump(mode="json"), str(idempotency_key)
    )


@router.get("/orders/mine", response_model=CursorPage[OrderResponse])
async def read_my_orders(
    caller: AccessTokenDep,
    orders: OrderServiceDep,
    as_party: Annotated[
        Literal["buyer", "seller"],
        Query(alias="as", description="Which side of the order the caller is on."),
    ],
    status_filter: Annotated[
        OrderStatus | None,
        Query(
            alias="status",
            description="Only orders in this state. Omit for all of them.",
        ),
    ] = None,
    cursor: Annotated[str | None, Query(description="Opaque; echo the last one given.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> CursorPage[OrderResponse]:
    """The caller's own orders on one side, newest first.

    Declared before `/orders/{order_id}` because both live under the same prefix and a route that
    matched "mine" as an identity would answer 422 about a UUID instead of returning the list.

    Keyset-paginated rather than by offset, so an order created while somebody is reading their
    first page does not push a row they have already seen onto the next one.
    """
    data, next_cursor, has_more = await orders.read_mine(
        caller.user_id,
        buyer=as_party == "buyer",
        status_filter=status_filter,
        cursor=cursor,
        limit=limit,
    )
    return CursorPage[OrderResponse](data=data, next_cursor=next_cursor, has_more=has_more)


@router.get("/orders/{order_id}", response_model=OrderResponse)
async def read_order(
    caller: AccessTokenDep,
    orders: OrderServiceDep,
    order_id: UUID = OrderIdPath,
) -> OrderResponse:
    """One order, for the buyer and the seller of it.

    Reading it is also how a lapsed funding window is enforced, which is why this is a read that
    writes: the answer would otherwise be that the order is cancelled while its kilos are still
    held against the seller's listing.
    """
    return await orders.read(order_id, caller.user_id)
