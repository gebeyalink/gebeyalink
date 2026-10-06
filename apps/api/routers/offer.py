"""Bidding on a listing, and the two ways a negotiation ends.

Five routes for one resource, and none of them takes a status. A bid is placed, answered, or taken
back; it is never *set* to something. That keeps the state table in the service where every move has
to go through it, rather than spread across routes as a parameter a client could send.

The sixth, `POST /offers/{id}/accept`, is in the orders router and not here: it is the one move out
of this resource that writes a row of another kind. It creates an order, reserves stock, and moves
the listing in a single transaction, so it belongs beside the orders it produces rather than here,
where a reader would come looking only to find the route elsewhere.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Path, status

from apps.api.dependencies import AccessTokenDep, OfferServiceDep
from apps.api.schemas.offer import OfferCounterBody, OfferCreateBody, OfferResponse

router = APIRouter(tags=["Offers"])

OfferIdPath = Path(description="The offer's internal identity.")
ListingIdPath = Path(description="The listing's internal identity, not its short code.")


@router.post(
    "/listings/{listing_id}/offers",
    response_model=OfferResponse,
    status_code=status.HTTP_201_CREATED,
)
async def place_offer(
    body: OfferCreateBody,
    caller: AccessTokenDep,
    offers: OfferServiceDep,
    listing_id: UUID = ListingIdPath,
) -> OfferResponse:
    """Put terms on a live listing.

    The buyer states weight, price and how long the bid should stand; the ceiling, the weight
    floor, the deadline and the thread the bid belongs to are all worked out here rather than
    asked for.
    """
    return await offers.place(listing_id, caller.user_id, body.model_dump(mode="json"))


@router.get("/listings/{listing_id}/offers", response_model=list[OfferResponse])
async def read_listing_offers(
    caller: AccessTokenDep,
    offers: OfferServiceDep,
    listing_id: UUID = ListingIdPath,
) -> list[OfferResponse]:
    """Every offer on a listing, for the seller who owns it.

    The seller's alone, because a bid is a price somebody is willing to pay and one buyer
    reading the others would learn a floor instead of competing.
    """
    return await offers.read_for_listing(listing_id, caller.user_id)


@router.post("/offers/{offer_id}/reject", response_model=OfferResponse)
async def reject_offer(
    caller: AccessTokenDep,
    offers: OfferServiceDep,
    offer_id: UUID = OfferIdPath,
) -> OfferResponse:
    """The seller's no.

    Always available while the offer is open, including when the listing behind it has since
    paused or closed: refusing costs nothing and reserves nothing, so there is no reason a
    seller should be unable to say it.
    """
    return await offers.reject(offer_id, caller.user_id)


@router.post("/offers/{offer_id}/counter", response_model=OfferResponse)
async def counter_offer(
    body: OfferCounterBody,
    caller: AccessTokenDep,
    offers: OfferServiceDep,
    offer_id: UUID = OfferIdPath,
) -> OfferResponse:
    """Answer an open offer with different terms.

    Written by the party who did not make the offer being answered, and only that party — so
    the two sides of a thread alternate without either of them naming whose turn it is.
    """
    return await offers.counter(offer_id, caller.user_id, body.model_dump(mode="json"))


@router.post("/offers/{offer_id}/withdraw", response_model=OfferResponse)
async def withdraw_offer(
    caller: AccessTokenDep,
    offers: OfferServiceDep,
    offer_id: UUID = OfferIdPath,
) -> OfferResponse:
    """The buyer taking their own bid back off the table.

    The buyer's alone, and at any depth in a thread. A seller who wants a negotiation to stop
    rejects, which reads to the buyer as an answer rather than as them changing their mind.
    """
    return await offers.withdraw(offer_id, caller.user_id)
