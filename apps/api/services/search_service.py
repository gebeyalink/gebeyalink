"""Search: what a page of the market is allowed to mean, before anything is asked of data.

The repository builds one statement. This decides what that statement may ask. Three of
those decisions are rules the specification states as conditions rather than as shapes -
a radius needs a point to measure from, an order needs to be the one that produced the
cursor, and a boundary has to be complete enough to say where the last page stopped - and
all three belong where the request is still a request rather than a query.

Two smaller decisions are worth naming here because they are easy to get subtly wrong.

The trader's own point is never read into Python. Whether they have one is asked of the
database, distance is computed in the statement, and the measured distance never appears
in the response: it travels only inside the cursor that carries the page. FR-404 publishes
a woreda and nothing finer, and a number that measures two traders' own points against each
other would say more about where each of them stands than any result is meant to.

The cursor carries the order that produced it. A trader who pages with nothing but a cursor
keeps the order they were already in, and a trader who states a different order is told the
two disagree rather than handed a page whose cursor means something else. The
specification's default still applies whenever there is nothing to inherit: no cursor, or a
cursor that says nothing about order, means distance.
"""

from __future__ import annotations

import base64
from datetime import date, datetime
from uuid import UUID

from apps.api.repositories.listing_repository import ListingRepository, LoadedListing
from apps.api.repositories.profile_repository import ProfileRepository
from apps.api.repositories.reservation_repository import ReservationRepository
from apps.api.repositories.search_repository import (
    PagePosition,
    SearchedListing,
    SearchFilters,
    SearchQuery,
    SearchRepository,
)
from apps.api.schemas.listing import ListingResponse
from apps.api.schemas.search import SearchRequest, SearchSort
from apps.api.services.listing_service import to_response
from packages.shared.errors import ApiError, ErrorCode

#: FR-401 gives ``radius_km`` a default of 50. The default is applied only once it is known
#: the trader has a point to measure from: a radius is a distance from somewhere, and there
#: is no somewhere for a trader who has never said where they are.
DEFAULT_RADIUS_KM = 50

#: Metres in a kilometre, kept beside the default so the conversion has one home.
METRES_PER_KILOMETRE = 1_000


class SearchService:
    def __init__(
        self,
        search: SearchRepository,
        listings: ListingRepository,
        reservations: ReservationRepository,
        profiles: ProfileRepository,
    ) -> None:
        self._search = search
        self._listings = listings
        self._reservations = reservations
        self._profiles = profiles

    async def search(
        self, buyer_id: UUID, request: SearchRequest
    ) -> tuple[list[ListingResponse], str | None, bool]:
        """One page of the market as this trader may see it, and how to ask for the next.

        Availability is read for the page as a whole rather than per listing, because the
        alternative is one query per row of every page, and the kilos a buyer cares about
        are the reason a listing is in the results at all.
        """
        has_point = await self._profiles.has_gps(buyer_id)
        if request.radius_km is not None and not has_point:
            # Refused rather than ignored. A trader who asked to search within fifty
            # kilometres and is handed the whole country has been given an answer to a
            # question they did not ask, and would have no way to notice.
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={"fields": {"radius_km": "requires_your_location"}},
            )
        after = _decode(request.cursor) if request.cursor is not None else None
        sort = _resolve_sort(request.sort, after)
        radius_m = None
        if has_point:
            kilometres = request.radius_km if request.radius_km is not None else DEFAULT_RADIUS_KM
            radius_m = float(kilometres * METRES_PER_KILOMETRE)

        rows, has_more = await self._search.page(
            SearchQuery(
                filters=SearchFilters(
                    crop_id=request.crop_id,
                    region_code=request.region_code,
                    woreda_id=request.woreda_id,
                    price_min=request.price_min,
                    price_max=request.price_max,
                    harvest_date_from=request.harvest_date_from,
                    harvest_date_to=request.harvest_date_to,
                    organic=request.organic,
                    delivery_option=request.delivery_option,
                    seller_tier_min=request.seller_tier_min,
                    radius_m=radius_m,
                ),
                sort=sort,
                after=after,
                limit=request.limit,
                buyer_id=buyer_id if has_point else None,
            )
        )
        listings = [row.listing for row in rows]
        photographs = await self._listings.photos_for([listing.id for listing in listings])
        reserved = await self._reservations.reserved_kg_for_listings(
            [listing.id for listing in listings]
        )
        data = [
            to_response(
                LoadedListing(listing=listing, photos=photographs.get(listing.id, [])),
                available_kg=listing.quantity_kg - reserved.get(listing.id, 0),
            )
            for listing in listings
        ]
        next_cursor = _encode(sort, rows[-1]) if has_more and rows else None
        return data, next_cursor, has_more


def _resolve_sort(stated: SearchSort | None, after: PagePosition | None) -> SearchSort:
    """The order this page is in, and the refusal of an order that contradicts its cursor.

    A cursor means one thing only under the order that produced it. Adopting the cursor's
    order when none is stated is what lets a trader page through results without repeating
    themselves; refusing a *stated* order that disagrees is what stops a client from
    believing it changed the order when it only relabelled the page.
    """
    if after is None:
        return stated or SearchSort.DISTANCE_ASC
    if stated is None or stated is after.sort:
        return after.sort
    raise ApiError(
        ErrorCode.VALIDATION_ERROR,
        details={"fields": {"sort": "does_not_match_the_cursor"}},
    )


def _encode(sort: SearchSort, row: SearchedListing) -> str:
    """Where a page ended, in a form the caller carries without reading.

    Six parts: the order, the time, the identity, the price, the harvest day, and the
    measured distance. The sort key travels because price and harvest are what two of the
    orders are by; the distance travels because it is nowhere else a caller could keep it.

    ``None`` travels as an empty part rather than as the word "none", so that a boundary
    which has no distance - a woreda nobody has mapped - reads back as no distance instead
    of as a measurement of nothing.
    """
    listing = row.listing
    distance = "" if row.distance_m is None else repr(row.distance_m)
    raw = "|".join(
        [
            sort.value,
            listing.created_at.isoformat(),
            str(listing.id),
            str(listing.unit_price_santim),
            listing.harvest_date.isoformat(),
            distance,
        ]
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(cursor: str) -> PagePosition:
    """Read a page boundary back, refusing one this server did not write.

    The shape is checked here rather than in the statement, because a boundary is only
    usable if every part of it could be parsed into what the order needs: an unparseable
    token, an unknown order, or a row count of fields that does not add up are all a client
    sending something it did not get from us.
    """
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        sort_value, stamp, identity, price, harvest, distance = raw.split("|")
        return PagePosition(
            sort=SearchSort(sort_value),
            created_at=datetime.fromisoformat(stamp),
            id=UUID(identity),
            unit_price_santim=int(price) if price else None,
            harvest_date=date.fromisoformat(harvest) if harvest else None,
            distance_m=float(distance) if distance else None,
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, details={"fields": {"cursor": "not_a_valid_cursor"}}
        ) from exc
