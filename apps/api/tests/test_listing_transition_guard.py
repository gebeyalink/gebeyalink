"""The state table, read on its own.

Every transition the specification allows, and every one it does not, checked here against
the guard rather than through HTTP. The point is that the table is complete: a move that is
missing from this file is a move nobody can make, and a move that is in it is a move nobody
has to think about.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

# No HTTP, no database, no clock beyond the moment handed in: this file exercises the
# arithmetic of the guard alone.
pytestmark = pytest.mark.unit

from apps.api.models.listing import Listing, ListingSource, ListingStatus  # noqa: E402
from apps.api.services.listing_service import (  # noqa: E402
    ALLOWED_TRANSITIONS,
    effective_status,
    transition_listing_status,
)
from packages.shared.errors import ApiError, ErrorCode  # noqa: E402

NOW = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)

#: Nobody in particular. The guard reads only the status and the expiry, so whose listing it
#: is makes no difference and is not worth inventing an account for.
SELLER = uuid.UUID("00000000-0000-0000-0000-000000000001")

#: The whole table, exactly as the specification states it. Written out independently of the
#: implementation so that a rule quietly added to the service fails here rather than passing.
SPEC_TABLE: list[tuple[ListingStatus, ListingStatus]] = [
    (ListingStatus.DRAFT, ListingStatus.ACTIVE),
    (ListingStatus.ACTIVE, ListingStatus.PAUSED),
    (ListingStatus.PAUSED, ListingStatus.ACTIVE),
    (ListingStatus.ACTIVE, ListingStatus.SOLD_OUT),
    (ListingStatus.ACTIVE, ListingStatus.EXPIRED),
    (ListingStatus.PAUSED, ListingStatus.EXPIRED),
    (ListingStatus.ACTIVE, ListingStatus.SUSPENDED),
    (ListingStatus.SUSPENDED, ListingStatus.ACTIVE),
    *[
        (origin, ListingStatus.CLOSED)
        for origin in (
            ListingStatus.DRAFT,
            ListingStatus.ACTIVE,
            ListingStatus.PAUSED,
            ListingStatus.SOLD_OUT,
            ListingStatus.EXPIRED,
            ListingStatus.SUSPENDED,
        )
    ],
]


def a_listing(status: ListingStatus, expires_at: datetime | None = None) -> Listing:
    """A listing in a given state, unstored, with an expiry comfortably in the future."""
    return Listing(
        seller_id=SELLER,
        source=ListingSource.APP,
        crop_id=1,
        quantity_kg=100,
        min_order_kg=1,
        unit_price_santim=100,
        harvest_date=NOW.date(),
        expires_at=expires_at or (NOW + timedelta(days=30)),
        region_code="OR",
        woreda_id=1,
        delivery_options=[],
        short_code="ABC234",
        status=status,
    )


@pytest.mark.parametrize(("origin", "target"), SPEC_TABLE, ids=lambda value: value.value)
def test_every_move_the_state_table_allows_is_allowed(
    origin: ListingStatus, target: ListingStatus
) -> None:
    """Each allowed pair, and only allowed pairs, goes through."""
    listing = a_listing(origin)

    assert transition_listing_status(listing, target) is target
    assert listing.status is target


@pytest.mark.parametrize("origin", list(ListingStatus))
def test_no_state_may_move_to_itself(origin: ListingStatus) -> None:
    """Staying put is not a transition.

    It reads as though it should be allowed, and allowing it would let a caller bump a
    version without having changed anything.
    """
    listing = a_listing(origin)

    with pytest.raises(ApiError) as raised:
        transition_listing_status(listing, origin)

    assert raised.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION
    assert listing.status is origin


@pytest.mark.parametrize("target", [status for status in ListingStatus if status.value])
def test_a_closed_listing_goes_nowhere_at_all(target: ListingStatus) -> None:
    """Closing is the end of the row. There is nothing after it, including a reopen."""
    listing = a_listing(ListingStatus.CLOSED)

    with pytest.raises(ApiError) as raised:
        transition_listing_status(listing, target)

    assert raised.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION


@pytest.mark.parametrize(
    ("origin", "target"),
    [
        (ListingStatus.DRAFT, ListingStatus.PAUSED),
        (ListingStatus.PAUSED, ListingStatus.SUSPENDED),
        (ListingStatus.SOLD_OUT, ListingStatus.ACTIVE),
        (ListingStatus.EXPIRED, ListingStatus.ACTIVE),
        (ListingStatus.EXPIRED, ListingStatus.PAUSED),
        (ListingStatus.SUSPENDED, ListingStatus.PAUSED),
    ],
    ids=lambda value: value.value,
)
def test_the_moves_the_table_does_not_list_are_refused(
    origin: ListingStatus, target: ListingStatus
) -> None:
    """The near misses, which are the ones a client would plausibly try."""
    listing = a_listing(origin)

    with pytest.raises(ApiError) as raised:
        transition_listing_status(listing, target)

    assert raised.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION
    assert listing.status is origin, "a refused move must leave the listing where it was"


def test_a_refusal_says_where_the_listing_was_and_where_it_was_asked_to_go() -> None:
    """A client told only that a move is illegal cannot tell which one is wrong."""
    listing = a_listing(ListingStatus.PAUSED)

    with pytest.raises(ApiError) as raised:
        transition_listing_status(listing, ListingStatus.SUSPENDED)

    assert raised.value.details["status"] == "PAUSED"
    assert raised.value.details["requested"] == "SUSPENDED"


def test_the_table_covers_every_status() -> None:
    """A status the table does not mention would be one nothing could ever do."""
    assert set(ALLOWED_TRANSITIONS) == set(ListingStatus)


@pytest.mark.parametrize(
    ("status", "lapsed", "expected"),
    [
        (ListingStatus.ACTIVE, True, ListingStatus.EXPIRED),
        (ListingStatus.ACTIVE, False, ListingStatus.ACTIVE),
        (ListingStatus.PAUSED, True, ListingStatus.EXPIRED),
        (ListingStatus.PAUSED, False, ListingStatus.PAUSED),
        (ListingStatus.CLOSED, True, ListingStatus.CLOSED),
        (ListingStatus.SUSPENDED, True, ListingStatus.SUSPENDED),
        (ListingStatus.SOLD_OUT, True, ListingStatus.SOLD_OUT),
    ],
    ids=lambda value: value.value if isinstance(value, ListingStatus) else str(value),
)
def test_a_listing_past_its_expiry_has_already_expired(
    status: ListingStatus, lapsed: bool, expected: ListingStatus
) -> None:
    """Expiry is read against the clock, so nothing has to have run for it to be true.

    Only the two states the table names can lapse: a closed or suspended listing has already
    reached a state of its own, and calling it expired would hide that.
    """
    expires_at = NOW - timedelta(seconds=1) if lapsed else NOW + timedelta(days=1)
    listing = a_listing(status, expires_at=expires_at)

    assert effective_status(listing, now=NOW) is expected


def test_the_moment_of_expiry_belongs_to_the_expired_side() -> None:
    """A listing expiring exactly now is over, not still running for another instant."""
    listing = a_listing(ListingStatus.ACTIVE, expires_at=NOW)

    assert effective_status(listing, now=NOW) is ListingStatus.EXPIRED


def test_a_lapsed_listing_cannot_be_paused_through_the_back_door() -> None:
    """The guard reads the derived state, so an expired listing is refused on every path.

    Checking the stored status instead would let a pause land on a listing nobody should be
    able to touch, and only on the requests that happened to be first.
    """
    listing = a_listing(ListingStatus.ACTIVE, expires_at=NOW - timedelta(hours=1))

    with pytest.raises(ApiError) as raised:
        transition_listing_status(listing, ListingStatus.PAUSED)

    assert raised.value.details["status"] == "EXPIRED"
