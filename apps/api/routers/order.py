"""Five routes: two ways to create an order, one to end it, and two to read them.

An order is created in exactly two ways and both of them are here, because they are the same
transaction with one row missing: accepting an offer that already agreed a price and weight, and
buying now at the asking price. The money is not in the body of either, and `extra="forbid"` on
the schemas is what makes that true rather than aspirational — a client sending `total_santim`
is refused by name instead of being quietly ignored.

Acceptance lives on the offer rather than on the order, and buy-now on the listing rather than
on the order, because that is where the decision was made. FR-608's buy-now path is
`/listings/:id/buy-now` and an acceptance belongs to the offer's own history; an order is the
consequence of both and gets no route of its own for something it did not decide.

Cancellation takes no body. There is nothing for a buyer to say that the platform would record
differently — the reason is `BUYER_CANCELLED` because a buyer cancelling and a funding window
closing are different facts about the future, and one word for both would lose that — and a body
that could carry a reason would be a body whose contents nothing acts on.

The three mutations require `Idempotency-Key`. A buyer on a slow connection taps twice and means
it once, and the second tap must not be a second order.

The reads answer the same question from two directions — "what is happening to my crops" — and
both are scoped to the caller's own party on an order, because an order is the only row in the
platform that names two accounts. It is also where a buyer's address and a seller's committed
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
from apps.api.schemas.order import OrderAcceptBody, OrderBuyNowBody, OrderResponse
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
    """The buyer giving up on an order they have not paid for, and the kilos going back.

    Only while the order is still waiting for money. The seller cannot do this — an order is what
    acceptance creates, and FR-607 gives a seller their way to stop a bid before it becomes one,
    which is rejecting it.
    """
    return await orders.cancel(order_id, caller.user_id, str(idempotency_key))


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
