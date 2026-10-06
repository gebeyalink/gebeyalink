"""Finding what is for sale.

One endpoint, and every parameter in it is a question a buyer already knows how to ask:
which crop, which region, how far, at what price, harvested when. The route names what the
contract publishes, hands it to the service unchanged, and puts the answer in the page
envelope every paginated endpoint in this API answers with.

What the route deliberately does *not* do is decide anything. Whether a radius may be
asked at all, what order a cursor belongs to, which states of a listing are the market's
business - those are rules about the trader and the data rather than about a query string,
and a rule that lived here would apply only to the HTTP door into a service that the SMS
and USSD doors are meant to use too.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from apps.api.dependencies import AccessTokenDep, SearchServiceDep
from apps.api.models.listing import DeliveryOption
from apps.api.models.user import VerificationTier
from apps.api.schemas.listing import ListingResponse
from apps.api.schemas.pagination import CursorPage
from apps.api.schemas.search import RegionCode, SearchRequest, SearchSort

router = APIRouter(prefix="/search", tags=["Search"])

#: The page size ceiling from the pagination convention: beyond it a single response
#: carries more crop than a phone on a slow connection should be asked to render.
MAX_PAGE_SIZE = 100


@router.get("/listings", response_model=CursorPage[ListingResponse])
async def search_listings(
    caller: AccessTokenDep,
    search: SearchServiceDep,
    crop_id: Annotated[int | None, Query(description="One crop, by the taxonomy's number.")] = None,
    region_code: Annotated[
        RegionCode | None, Query(description="One of the thirteen regions.")
    ] = None,
    woreda_id: Annotated[int | None, Query(description="One woreda, by its internal id.")] = None,
    radius_km: Annotated[
        int | None, Query(ge=1, le=500, description="How far from the trader's own point.")
    ] = None,
    price_min: Annotated[int | None, Query(description="Lowest unit price, in santim.")] = None,
    price_max: Annotated[int | None, Query(description="Highest unit price, in santim.")] = None,
    harvest_date_from: Annotated[date | None, Query(description="Earliest harvest day.")] = None,
    harvest_date_to: Annotated[date | None, Query(description="Latest harvest day.")] = None,
    organic: Annotated[
        bool | None, Query(description="Whether the crop was grown without inputs.")
    ] = None,
    delivery_option: Annotated[
        DeliveryOption | None, Query(description="How the buyer would take it away.")
    ] = None,
    seller_tier_min: Annotated[
        VerificationTier | None, Query(description="The lowest verified tier a seller may be.")
    ] = None,
    sort: Annotated[
        SearchSort | None,
        Query(description="Order; when a cursor is sent, the order it already carries."),
    ] = None,
    cursor: Annotated[str | None, Query(description="Opaque; echo the last one given.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> CursorPage[ListingResponse]:
    """One page of the market, filtered and ordered the way the caller asked.

    Open to every signed-in trader rather than to buyers alone: a search is a read of what
    is on offer, and this API does not yet have a reason to keep one trader from seeing
    what every trader can already see in the listings themselves.
    """
    data, next_cursor, has_more = await search.search(
        caller.user_id,
        SearchRequest(
            crop_id=crop_id,
            region_code=region_code,
            woreda_id=woreda_id,
            radius_km=radius_km,
            price_min=price_min,
            price_max=price_max,
            harvest_date_from=harvest_date_from,
            harvest_date_to=harvest_date_to,
            organic=organic,
            delivery_option=delivery_option,
            seller_tier_min=seller_tier_min,
            sort=sort,
            cursor=cursor,
            limit=limit,
        ),
    )
    return CursorPage[ListingResponse](data=data, next_cursor=next_cursor, has_more=has_more)
