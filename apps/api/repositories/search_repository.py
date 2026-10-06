"""Listing discovery: one statement that answers a whole search.

Everything a search asks for - which rows qualify, where the page it is on begins and ends,
and what order those rows are in - is built here as a single statement, because those three
questions cannot be answered separately. The order is what gives a cursor its meaning, so a
cursor produced under one order and replayed under another points at a different row than
the one it was issued for, and a page built from it would quietly repeat or drop a listing.

Four things about the statement are decisions rather than mechanics, and each is stated
where it is made rather than left to be discovered from the SQL.

Every filter is ANDed, and nothing else combines them. A search narrows: a buyer who asks
for one crop in one region wants the listings that are both, and a filter that quietly lost
an argument it contradicted would answer a question nobody asked.

Distance is computed in the statement. A listing has no coordinates of its own; the finest
place the platform is told is the woreda it names, and the buyer's own point is read inside
the statement as a subquery rather than passed through Python, so that no raw coordinate is
ever carried into a query as a literal.

What may be shown is decided by FR-404's five exclusions plus the clock, not by a status
list alone. Expiry is read rather than written - a listing past its lifetime still says
`ACTIVE` in its row until somebody looks at it - so excluding `EXPIRED` means excluding
rows the clock has taken over, not rows the column names.

The page is cut at ``limit + 1`` rows. Whether more remains is then answered by the rows
themselves rather than by a second count of everything that ever matched, which would cost
more than the page and be stale the moment it was computed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.listing import DeliveryOption, Listing, ListingStatus
from apps.api.models.profile import Profile
from apps.api.models.user import User, VerificationTier
from apps.api.models.woreda import Woreda
from apps.api.schemas.search import SearchSort
from packages.shared.errors import ApiError, ErrorCode

#: The label the measured distance travels under when it is part of the page's rows.
DISTANCE_COLUMN = "distance_m"

#: FR-401's ``seller_tier_min`` is a floor, and the tiers are named rather than numbered,
#: so the floor is written out as the set it means instead of being compared as a string
#: that happens to sort in the right order.
_TIERS_AT_OR_ABOVE: dict[VerificationTier, tuple[VerificationTier, ...]] = {
    VerificationTier.TIER_0: (
        VerificationTier.TIER_0,
        VerificationTier.TIER_1,
        VerificationTier.TIER_2,
    ),
    VerificationTier.TIER_1: (VerificationTier.TIER_1, VerificationTier.TIER_2),
    VerificationTier.TIER_2: (VerificationTier.TIER_2,),
}


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """FR-401's predicates, already validated and already in the units the columns hold.

    Every field is optional and every field that is present is ANDed with the rest. Two
    ranges that contradict each other - a minimum above a maximum, a harvest window with
    nothing in it - mean nothing is there to show, which is the honest answer to a question
    that has no rows in it rather than an error the caller did not cause.
    """

    crop_id: int | None = None
    region_code: str | None = None
    woreda_id: int | None = None
    price_min: int | None = None
    price_max: int | None = None
    harvest_date_from: date | None = None
    harvest_date_to: date | None = None
    organic: bool | None = None
    delivery_option: DeliveryOption | None = None
    seller_tier_min: VerificationTier | None = None
    #: Metres, already multiplied out, and already known to be askable: a trader with no
    #: point has no radius and is never given one. ``None`` means no distance is enforced.
    radius_m: float | None = None


@dataclass(frozen=True, slots=True)
class PagePosition:
    """Where the previous page stopped, in the units of the order that stopped it.

    Time and identity travel for every order, because every order here falls back to them
    to break ties and no two listings share an identity. The sort key travels for the
    orders whose key is not time, and the measured distance travels for the order that
    measures: it is not in the response at all, so a cursor is the only place a caller
    could carry it from.
    """

    sort: SearchSort
    created_at: datetime
    id: uuid.UUID
    unit_price_santim: int | None = None
    harvest_date: date | None = None
    distance_m: float | None = None


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """One search, as the repository is to execute it."""

    filters: SearchFilters
    sort: SearchSort
    after: PagePosition | None
    limit: int
    #: The trader whose own point distance is measured from, or none when they have given
    #: no point and distance therefore has no meaning for this request.
    buyer_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class SearchedListing:
    """A row of a page, and its distance when the statement measured one."""

    listing: Listing
    distance_m: float | None


class SearchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def page(self, query: SearchQuery) -> tuple[list[SearchedListing], bool]:
        """One page of the market, and whether more of it remains."""
        result = await self._session.execute(_statement(query))
        fetched = list(result.all())
        rows = [
            SearchedListing(listing=row[0], distance_m=row[1] if len(row) > 1 else None)
            for row in fetched[: query.limit]
        ]
        return rows, len(fetched) > query.limit


def _statement(query: SearchQuery) -> Select[Any]:
    """The whole request as one statement: filters, boundary, order, page size."""
    filters = query.filters
    now = datetime.now(UTC)
    distance = _distance_from(query.buyer_id) if query.buyer_id is not None else None

    if distance is not None and query.sort is SearchSort.DISTANCE_ASC:
        stmt: Select[Any] = select(Listing, distance.label(DISTANCE_COLUMN))
    else:
        stmt = select(Listing)
    stmt = stmt.where(_on_the_market(now))
    if distance is not None:
        stmt = stmt.join(Woreda, Woreda.id == Listing.woreda_id)
    if filters.crop_id is not None:
        stmt = stmt.where(Listing.crop_id == filters.crop_id)
    if filters.region_code is not None:
        stmt = stmt.where(Listing.region_code == filters.region_code)
    if filters.woreda_id is not None:
        stmt = stmt.where(Listing.woreda_id == filters.woreda_id)
    if filters.price_min is not None:
        stmt = stmt.where(Listing.unit_price_santim >= filters.price_min)
    if filters.price_max is not None:
        stmt = stmt.where(Listing.unit_price_santim <= filters.price_max)
    if filters.harvest_date_from is not None:
        stmt = stmt.where(Listing.harvest_date >= filters.harvest_date_from)
    if filters.harvest_date_to is not None:
        stmt = stmt.where(Listing.harvest_date <= filters.harvest_date_to)
    if filters.organic is not None:
        stmt = stmt.where(Listing.organic == filters.organic)
    if filters.delivery_option is not None:
        stmt = stmt.where(Listing.delivery_options.contains([filters.delivery_option]))
    if filters.seller_tier_min is not None:
        stmt = stmt.join(User, User.id == Listing.seller_id).where(
            User.kyc_tier.in_(_TIERS_AT_OR_ABOVE[filters.seller_tier_min])
        )
    if distance is not None and filters.radius_m is not None:
        # A woreda whose boundary has not arrived yet has no distance at all. NULL compares
        # to nothing, so such a listing falls outside every radius rather than quietly
        # passing it - and when the boundary dataset lands, no code changes here: the
        # distance simply starts being a number and the listing starts being measured.
        stmt = stmt.where(distance <= filters.radius_m)
    if query.after is not None:
        stmt = stmt.where(_beyond(distance, query.after))
    stmt = stmt.order_by(*_order_by(distance, query.sort)).limit(query.limit + 1)
    return stmt


def _on_the_market(now: datetime) -> ColumnElement[bool]:
    """What FR-404 allows a buyer to see, as a predicate.

    The five excluded states are `DRAFT`, `PAUSED`, `EXPIRED`, `CLOSED` and `SUSPENDED`,
    but they cannot be matched against the status column alone: expiry is read rather than
    written, so a listing whose lifetime has run out still says `ACTIVE` in its row until
    somebody looks at it (FR-306 is explicit that a read must not write). The clock is
    therefore part of this predicate.

    A `SOLD_OUT` listing whose time has also run out stays visible, because that is how it
    reads: the clock only takes a listing over from `ACTIVE` and `PAUSED`, so an exhausted
    listing is still an exhausted listing no matter when it was posted.
    """
    return or_(
        and_(Listing.status == ListingStatus.ACTIVE, Listing.expires_at > now),
        Listing.status == ListingStatus.SOLD_OUT,
    )


def _distance_from(buyer_id: uuid.UUID) -> ColumnElement[Any]:
    """Metres from this trader's own point to each listing's woreda.

    Measured to the woreda's centroid rather than to the listing: a listing carries no
    coordinate, and the woreda is the finest place the platform is told. The trader's point
    is read here as a correlated subquery so that the coordinate never leaves the database
    as a literal - a value that appears in a statement as text is a value somebody can
    accidentally build, and geography is not a value this code needs to hold.
    """
    center = select(Profile.gps).where(Profile.user_id == buyer_id).scalar_subquery()
    return func.ST_Distance(func.ST_Centroid(Woreda.geog), center)


def _order_by(
    distance: ColumnElement[Any] | None, sort: SearchSort
) -> tuple[ColumnElement[Any], ...]:
    """The order a page is in, and the tie-break that makes it total.

    Every branch ends in `id`, because nothing else here is guaranteed distinct: two
    listings posted in the same instant, at the same price, from the same harvest day are
    an ordinary Tuesday on a marketplace, and an order that ties on all of them is an order
    that cannot say which row comes next.
    """
    if sort is SearchSort.DISTANCE_ASC and distance is not None:
        # Unknown distance last: a woreda without a boundary is not nearer than one that
        # has one, it is unmeasured, and putting it first would answer a question about
        # geography with a question about which rows a dataset happens to have reached.
        return (
            distance.asc().nulls_last(),
            Listing.created_at.desc(),
            Listing.id.desc(),
        )
    if sort is SearchSort.PRICE_ASC:
        return (Listing.unit_price_santim.asc(), Listing.id.asc())
    if sort is SearchSort.PRICE_DESC:
        return (Listing.unit_price_santim.desc(), Listing.id.asc())
    if sort is SearchSort.HARVEST_DATE_ASC:
        return (Listing.harvest_date.asc(), Listing.id.asc())
    # The default order, and also what distance falls back to when the trader gave no point
    # to measure from: newest first is the one order that needs nothing from the caller.
    return (Listing.created_at.desc(), Listing.id.desc())


def _beyond(distance: ColumnElement[Any] | None, position: PagePosition) -> ColumnElement[bool]:
    """The rows strictly after where the previous page stopped, under that page's order.

    The boundary is the whole sort key and the identity, never the key alone: `price` ties
    are ordinary, and a predicate that stopped at the price would start the next page in
    the middle of a group it had already shown.
    """
    if position.sort is SearchSort.PRICE_ASC:
        price = _required(position.unit_price_santim, "unit_price_santim")
        return or_(
            Listing.unit_price_santim > price,
            and_(Listing.unit_price_santim == price, Listing.id > position.id),
        )
    if position.sort is SearchSort.PRICE_DESC:
        price = _required(position.unit_price_santim, "unit_price_santim")
        return or_(
            Listing.unit_price_santim < price,
            and_(Listing.unit_price_santim == price, Listing.id > position.id),
        )
    if position.sort is SearchSort.HARVEST_DATE_ASC:
        harvest = _required(position.harvest_date, "harvest_date")
        return or_(
            Listing.harvest_date > harvest,
            and_(Listing.harvest_date == harvest, Listing.id > position.id),
        )
    if position.sort is SearchSort.DISTANCE_ASC and distance is not None:
        return _beyond_distance(distance, position)
    return tuple_(Listing.created_at, Listing.id) < (position.created_at, position.id)


def _beyond_distance(distance: ColumnElement[Any], position: PagePosition) -> ColumnElement[bool]:
    """The rows after this one when the order is measured distance, then time, then nothing.

    Two shapes, because a measured page crosses two blocks. Measured rows come first in
    ascending order and the unmeasured ones stand after all of them, so a cursor standing in
    the measured block continues through the rest of that block and then into the
    unmeasured; a cursor standing in the unmeasured block has nothing after it but older
    unmeasured rows. Treating unmeasured as "infinitely far" would be the other reading, and
    it would put a woreda nobody has mapped ahead of one that is a hundred metres away.
    """
    time_and_identity = or_(
        Listing.created_at < position.created_at,
        and_(Listing.created_at == position.created_at, Listing.id < position.id),
    )
    cursor_distance = position.distance_m
    if cursor_distance is None:
        return and_(distance.is_(None), time_and_identity)
    return or_(
        distance > cursor_distance,
        and_(distance == cursor_distance, time_and_identity),
        distance.is_(None),
    )


def _required[T](value: T | None, field: str) -> T:
    """The part of a boundary this order cannot be built without.

    Raised as a validation failure rather than allowed through as a null: a cursor that
    claims to end at a price it did not carry is a cursor this server did not write, and
    the statement that followed it would be a statement about nothing.
    """
    if value is None:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, details={"fields": {"cursor": f"missing_{field}"}}
        )
    return value
