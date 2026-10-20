"""Posting, reading, editing and moving a listing.

The lifecycle endpoints are three one-word endpoints rather than one endpoint taking a status,
because the moves are not symmetric. Pause and resume are each legal from exactly one state,
and letting a client name the target would mean telling it the state table in order to know
whether its request would work. Three names mean the table stays in one place.

Nothing here decides anything. Each route hands a body to the service and returns what it
says, so that the same rules apply whether a listing arrives over HTTP today or from the SMS
and USSD paths tomorrow.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Path, Query, status

from apps.api.dependencies import AccessTokenDep, AgentServiceDep, ListingServiceDep
from apps.api.schemas.listing import ListingCreateBody, ListingResponse, ListingUpdateBody
from apps.api.schemas.pagination import CursorPage

router = APIRouter(prefix="/listings", tags=["Listings"])

#: The page size ceiling from the pagination convention. Beyond it a single response carries
#: more crop than a phone on a slow connection should be asked to render.
MAX_PAGE_SIZE = 100

ListingIdPath = Path(description="The listing's internal identity, not its short code.")


@router.post("", response_model=ListingResponse, status_code=status.HTTP_201_CREATED)
async def create_listing(
    body: ListingCreateBody,
    caller: AccessTokenDep,
    listings: ListingServiceDep,
    agents: AgentServiceDep,
) -> ListingResponse:
    """Post a listing, live.

    The seller states what they have and what they want for it; the status it starts in, the
    code it is quoted by and whether its price looks unusual are all decided here rather than
    asked for, because a client that could set its own status could post a listing nobody
    reviewed.

    One field changes who the seller is. When ``on_behalf_of_user_id`` is present the request
    is a delegated posting: it goes through the agent service, which checks the caller is an
    agent holding an active assignment for that farmer and writes the listing in the farmer's
    name. Without that field the caller is the seller, exactly as before.
    """
    payload = body.model_dump(mode="json")
    farmer_id = body.on_behalf_of_user_id
    if farmer_id is None:
        return await listings.create(caller.user_id, payload)
    return await agents.create_listing_on_behalf(
        agent_id=caller.user_id,
        farmer_id=farmer_id,
        body=payload,
    )


@router.get("/mine", response_model=CursorPage[ListingResponse])
async def read_my_listings(
    caller: AccessTokenDep,
    listings: ListingServiceDep,
    cursor: str | None = Query(default=None, description="Opaque; echo the last one given."),
    limit: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
) -> CursorPage[ListingResponse]:
    """The caller's own listings, newest first.

    Keyset-paginated rather than by offset, so a listing posted while somebody is reading
    their first page does not push a row they have already seen onto the next one.
    """
    data, next_cursor, has_more = await listings.read_mine(caller.user_id, cursor, limit)
    return CursorPage[ListingResponse](data=data, next_cursor=next_cursor, has_more=has_more)


@router.get("/{listing_id}", response_model=ListingResponse)
async def read_listing(
    caller: AccessTokenDep,
    listings: ListingServiceDep,
    listing_id: UUID = ListingIdPath,
) -> ListingResponse:
    """One listing, for any signed-in caller.

    Open to every signed-in caller rather than to buyers and sellers separately: the listing
    is an offer to the market, and hiding it from half the market is not a privacy measure.
    """
    return await listings.read(listing_id)


@router.patch("/{listing_id}", response_model=ListingResponse)
async def update_listing(
    body: ListingUpdateBody,
    caller: AccessTokenDep,
    listings: ListingServiceDep,
    listing_id: UUID = ListingIdPath,
) -> ListingResponse:
    """Change some of the seller's own listing.

    A merge, and a guarded one: the request says which version of the row it was written
    against, and if that is no longer current the write is refused and the current row is
    returned in the error so the caller can see what it collided with.
    """
    return await listings.update(listing_id, caller.user_id, body.model_dump(exclude_unset=True))


@router.post("/{listing_id}/pause", response_model=ListingResponse)
async def pause_listing(
    caller: AccessTokenDep,
    listings: ListingServiceDep,
    listing_id: UUID = ListingIdPath,
) -> ListingResponse:
    """Take a live listing off the market without closing it.

    It keeps its code, its photographs and its place in the seller's history, and it can be
    put back. Closing is the move that does not come back.
    """
    return await listings.pause(listing_id, caller.user_id)


@router.post("/{listing_id}/resume", response_model=ListingResponse)
async def resume_listing(
    caller: AccessTokenDep,
    listings: ListingServiceDep,
    listing_id: UUID = ListingIdPath,
) -> ListingResponse:
    """Put a paused listing back in front of buyers."""
    return await listings.resume(listing_id, caller.user_id)


@router.post("/{listing_id}/close", response_model=ListingResponse)
async def close_listing(
    caller: AccessTokenDep,
    listings: ListingServiceDep,
    listing_id: UUID = ListingIdPath,
) -> ListingResponse:
    """End a listing for good.

    Legal from any live state, which is the point of closing rather than pausing: a seller
    who has run out of crop, changed their mind, or finished the season should not have to
    work out which of those words the platform expects.
    """
    return await listings.close(listing_id, caller.user_id)
