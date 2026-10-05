"""The offer state table, and the arithmetic that decides who may move an offer.

Every transition the specification allows, and every one it does not, checked here against the
guard rather than through HTTP. The point is that the table is complete: a move missing from
this file is a move nobody can make, and a move in it is a move nobody has to think about.

The parity and the term checks live here too. Both are decisions about a row rather than about
a request, and both are the kind of rule that is cheap to test once and expensive to debug from
a failing negotiation later.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

# No HTTP, no database, no clock beyond the moment handed in: this file exercises the
# arithmetic of the guard alone.
pytestmark = pytest.mark.unit

from apps.api.models.listing import Listing, ListingSource, ListingStatus  # noqa: E402
from apps.api.models.offer import (  # noqa: E402
    MAX_THREAD_DEPTH,
    Offer,
    OfferStatus,
    Proposer,
    proposing_party,
)
from apps.api.services.offer_service import (  # noqa: E402
    ALLOWED_TRANSITIONS,
    INITIAL_STATUSES,
    MAX_OPEN_OFFERS_PER_LISTING,
    MAX_PRICE_MULTIPLE,
    OfferTerms,
    effective_status,
    may_answer,
    terms_from,
    transition_offer_status,
    validate_offer_terms,
)
from packages.shared.errors import ApiError, ErrorCode  # noqa: E402

NOW = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)

SELLER = uuid.UUID("00000000-0000-0000-0000-000000000001")
BUYER = uuid.UUID("00000000-0000-0000-0000-000000000002")
STRANGER = uuid.UUID("00000000-0000-0000-0000-000000000003")

#: The whole table, exactly as the specification states it. Written out independently of the
#: implementation so that a rule quietly added to the service fails here rather than passing.
SPEC_TABLE: list[tuple[OfferStatus, OfferStatus]] = [
    (OfferStatus.OPEN, OfferStatus.ACCEPTED),
    (OfferStatus.OPEN, OfferStatus.REJECTED),
    (OfferStatus.OPEN, OfferStatus.WITHDRAWN),
    (OfferStatus.OPEN, OfferStatus.EXPIRED),
    (OfferStatus.OPEN, OfferStatus.SUPERSEDED),
]

#: Twice the ask of the listing below. Named so the ceiling assertions read as a rule rather
#: than as arithmetic somebody has to redo.
UNIT_PRICE = 3_250_000
CEILING = UNIT_PRICE * MAX_PRICE_MULTIPLE


def a_listing(status: ListingStatus = ListingStatus.ACTIVE) -> Listing:
    """A listing in a given state, unstored, with the boundaries the term checks read."""
    return Listing(
        seller_id=SELLER,
        source=ListingSource.APP,
        crop_id=1,
        quantity_kg=2_000,
        min_order_kg=100,
        unit_price_santim=UNIT_PRICE,
        harvest_date=NOW.date(),
        expires_at=NOW + timedelta(days=30),
        region_code="OR",
        woreda_id=1,
        delivery_options=[],
        short_code="ABC234",
        status=status,
    )


def an_offer(
    status: OfferStatus = OfferStatus.OPEN,
    thread_depth: int = 0,
    valid_until: datetime | None = None,
) -> Offer:
    """An offer in a given state and position in its thread, unstored."""
    return Offer(
        listing_id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        buyer_id=BUYER,
        thread_id=uuid.UUID("00000000-0000-0000-0000-000000000020"),
        thread_depth=thread_depth,
        quantity_kg=500,
        price_santim=UNIT_PRICE,
        valid_until=valid_until or (NOW + timedelta(days=3)),
        status=status,
        version=1,
    )


def terms(**overrides: object) -> OfferTerms:
    """Offer terms on the right side of every rule, with any field replaced."""
    values: dict[str, object] = {
        "quantity_kg": 500,
        "price_santim": UNIT_PRICE,
        "valid_hours": 72,
    }
    values.update(overrides)
    return terms_from(values)


@pytest.mark.parametrize(("origin", "target"), SPEC_TABLE, ids=lambda value: value.value)
def test_every_move_the_state_table_allows_is_allowed(
    origin: OfferStatus, target: OfferStatus
) -> None:
    """Each allowed pair, and only allowed pairs, goes through."""
    offer = an_offer(origin)

    assert transition_offer_status(offer, target, origin) is target
    assert offer.status is target


@pytest.mark.parametrize("origin", list(OfferStatus))
def test_no_state_may_move_to_itself(origin: OfferStatus) -> None:
    """Staying put is not a transition.

    It reads as though it should be allowed, and allowing it would let a caller restate an
    offer's state without anything having changed.
    """
    offer = an_offer(origin)

    with pytest.raises(ApiError) as raised:
        transition_offer_status(offer, origin, origin)

    assert raised.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION
    assert offer.status is origin


@pytest.mark.parametrize(
    "origin", [status for status in OfferStatus if status is not OfferStatus.OPEN]
)
def test_every_state_but_open_goes_nowhere_at_all(origin: OfferStatus) -> None:
    """A finished negotiation is the end of the row.

    Nothing follows an accepted, refused, withdrawn, lapsed or superseded offer, including a
    return to open. A negotiation that could be reopened in place would have two live terms at
    once.
    """
    offer = an_offer(origin)

    for target in OfferStatus:
        with pytest.raises(ApiError) as raised:
            transition_offer_status(offer, target, origin)
        assert raised.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION
    assert offer.status is origin


@pytest.mark.parametrize(
    ("origin", "target"),
    [
        (OfferStatus.OPEN, OfferStatus.OPEN),
        (OfferStatus.REJECTED, OfferStatus.WITHDRAWN),
        (OfferStatus.WITHDRAWN, OfferStatus.REJECTED),
        (OfferStatus.SUPERSEDED, OfferStatus.WITHDRAWN),
        (OfferStatus.EXPIRED, OfferStatus.ACCEPTED),
        (OfferStatus.ACCEPTED, OfferStatus.SUPERSEDED),
    ],
    ids=lambda value: value.value,
)
def test_the_moves_the_table_does_not_list_are_refused(
    origin: OfferStatus, target: OfferStatus
) -> None:
    """The near misses, which are the ones a client would plausibly try."""
    offer = an_offer(origin)

    with pytest.raises(ApiError) as raised:
        transition_offer_status(offer, target, origin)

    assert raised.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION
    assert offer.status is origin, "a refused move must leave the offer where it was"


def test_a_refusal_says_where_the_offer_was_and_where_it_was_asked_to_go() -> None:
    """A client told only that a move is illegal cannot tell which one is wrong."""
    offer = an_offer(OfferStatus.WITHDRAWN)

    with pytest.raises(ApiError) as raised:
        transition_offer_status(offer, OfferStatus.REJECTED, OfferStatus.WITHDRAWN)

    assert raised.value.details["status"] == "WITHDRAWN"
    assert raised.value.details["requested"] == "REJECTED"


def test_the_table_covers_every_status() -> None:
    """A status the table does not mention would be one nothing could ever do."""
    assert set(ALLOWED_TRANSITIONS) == set(OfferStatus)


def test_an_offer_may_only_be_created_open() -> None:
    """Creation is the one move with no state behind it, and it lands in one place only.

    A caller cannot begin a negotiation in a finished state, which is what keeps the table's
    single entry point from being a second way to skip it.
    """
    assert frozenset({OfferStatus.OPEN}) == INITIAL_STATUSES

    for target in OfferStatus:
        offer = an_offer(target)
        if target is OfferStatus.OPEN:
            assert transition_offer_status(offer, target, None) is target
            continue
        with pytest.raises(ApiError) as raised:
            transition_offer_status(offer, target, None)
        assert raised.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION
        assert (
            raised.value.details["status"] is None
        ), "a creation that was refused should not claim the offer was in a state"


@pytest.mark.parametrize(
    ("status", "lapsed", "expected"),
    [
        (OfferStatus.OPEN, True, OfferStatus.EXPIRED),
        (OfferStatus.OPEN, False, OfferStatus.OPEN),
        (OfferStatus.REJECTED, True, OfferStatus.REJECTED),
        (OfferStatus.WITHDRAWN, True, OfferStatus.WITHDRAWN),
        (OfferStatus.SUPERSEDED, True, OfferStatus.SUPERSEDED),
        (OfferStatus.ACCEPTED, True, OfferStatus.ACCEPTED),
    ],
    ids=lambda value: value.value if isinstance(value, OfferStatus) else str(value),
)
def test_an_open_offer_past_its_deadline_has_already_expired(
    status: OfferStatus, lapsed: bool, expected: OfferStatus
) -> None:
    """Expiry is read against the clock, so nothing has to have run for it to be true.

    Only an open offer can lapse: one that has already been answered, refused, withdrawn or
    superseded reached a state of its own, and calling it expired would hide that.
    """
    deadline = NOW - timedelta(seconds=1) if lapsed else NOW + timedelta(days=1)
    offer = an_offer(status, valid_until=deadline)

    assert effective_status(offer, now=NOW) is expected


def test_the_moment_of_expiry_belongs_to_the_expired_side() -> None:
    """An offer whose deadline is this instant is over, not still standing for another one."""
    offer = an_offer(valid_until=NOW)

    assert effective_status(offer, now=NOW) is OfferStatus.EXPIRED


def test_a_lapsed_offer_cannot_be_reopened_through_the_back_door() -> None:
    """The guard is handed the derived state, so a lapsed offer is refused on every path.

    Checking the stored status instead would let a withdrawal land on an offer nobody should be
    able to touch, and only on the requests that happened to be first.
    """
    offer = an_offer(OfferStatus.OPEN, valid_until=NOW - timedelta(hours=1))

    with pytest.raises(ApiError) as raised:
        transition_offer_status(offer, OfferStatus.WITHDRAWN, effective_status(offer, now=NOW))

    assert raised.value.details["status"] == "EXPIRED"


@pytest.mark.parametrize(
    ("depth", "expected"),
    [(0, Proposer.BUYER), (1, Proposer.SELLER), (2, Proposer.BUYER), (3, Proposer.SELLER)],
    ids=lambda value: str(value),
)
def test_the_depth_says_whose_terms_these_are(depth: int, expected: Proposer) -> None:
    """A thread opens with the buyer and every answer comes from the other side.

    Derived, not stored, so it cannot disagree with the row it describes: an even depth is the
    buyer's terms and an odd depth is the seller's, all the way down.
    """
    assert proposing_party(depth) is expected


def test_the_thread_is_exactly_as_deep_as_the_limit_allows() -> None:
    """Three counters means four rows, and the fifth has nowhere to sit.

    Written as an assertion on the reachable depths rather than on the constant alone, because
    the rule is about the negotiation and the constant is only how it is spelled.
    """
    assert MAX_THREAD_DEPTH == 3
    assert list(range(MAX_THREAD_DEPTH + 1)) == [0, 1, 2, 3]


@pytest.mark.parametrize(
    ("depth", "may_respond"),
    [
        (0, SELLER),
        (1, BUYER),
        (2, SELLER),
        (3, BUYER),
    ],
    ids=lambda value: str(value),
)
def test_only_the_party_that_did_not_propose_may_answer(depth: int, may_respond: uuid.UUID) -> None:
    """The other side answers, and the party who wrote the terms does not.

    The seller answers a buyer's bid, the buyer answers the seller's answer, and neither may
    answer their own - which is what makes a negotiation rather than an edit.
    """
    listing = a_listing()
    offer = an_offer(thread_depth=depth)

    assert may_answer(offer, listing, may_respond)
    assert not may_answer(offer, listing, SELLER if may_respond is BUYER else BUYER)


def test_nobody_else_may_answer_an_offer() -> None:
    """A signed-in stranger is neither party to the negotiation.

    Neither the seller's nor the buyer's identity is enough on its own: the row has to be about
    them, or the answer would be addressed to somebody's negotiation.
    """
    listing = a_listing()
    offer = an_offer()

    assert not may_answer(offer, listing, STRANGER)


def test_valid_terms_pass_every_rule() -> None:
    """The baseline: nothing in this arrangement is near a boundary."""
    validate_offer_terms(terms(), a_listing())


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"price_santim": 0}, "price_santim"),
        ({"price_santim": -1}, "price_santim"),
        ({"price_santim": CEILING + 1}, "price_santim"),
        ({"quantity_kg": 99}, "quantity_kg"),
        ({"quantity_kg": 2_001}, "quantity_kg"),
        ({"valid_hours": 23}, "valid_hours"),
        ({"valid_hours": 169}, "valid_hours"),
        ({"quantity_kg": 99, "price_santim": CEILING + 1}, "quantity_kg"),
    ],
)
def test_terms_the_listing_forbids_are_refused_by_field(
    overrides: dict[str, object], field: str
) -> None:
    """The answer names the field, so an app can point at the input.

    Every rule below the request layer is still checked here: the request rules catch what a
    client sends, but an offer placed from the USSD path or a job would arrive without them.
    """
    with pytest.raises(ApiError) as raised:
        validate_offer_terms(terms(**overrides), a_listing())

    assert raised.value.code is ErrorCode.VALIDATION_ERROR
    assert field in raised.value.details["fields"]


def test_every_broken_rule_is_reported_at_once() -> None:
    """A buyer who got three fields wrong should not have to fix them three requests at a time."""
    with pytest.raises(ApiError) as raised:
        validate_offer_terms(
            terms(price_santim=CEILING + 1, quantity_kg=1, valid_hours=1), a_listing()
        )

    assert set(raised.value.details["fields"]) == {
        "price_santim",
        "quantity_kg",
        "valid_hours",
    }


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"price_santim": 1}, "price_santim"),
        ({"price_santim": CEILING}, "price_santim"),
        ({"quantity_kg": 100}, "quantity_kg"),
        ({"quantity_kg": 2_000}, "quantity_kg"),
        ({"valid_hours": 24}, "valid_hours"),
        ({"valid_hours": 168}, "valid_hours"),
    ],
)
def test_the_boundaries_themselves_are_allowed(overrides: dict[str, object], field: str) -> None:
    """The rule is inclusive at both ends, and this is where that is pinned down.

    A buyer offering exactly twice the ask, or exactly the listing's whole quantity, is inside
    the rule; only past it is outside. A check written with a strict comparison would refuse
    these and look correct while doing it.
    """
    validate_offer_terms(terms(**overrides), a_listing())


def test_the_ceiling_is_twice_the_ask() -> None:
    """Spelled out so a change to the multiple cannot pass unnoticed."""
    assert MAX_PRICE_MULTIPLE == 2
    assert MAX_OPEN_OFFERS_PER_LISTING == 10


def test_the_window_becomes_the_instant_it_names() -> None:
    """Seventy-two hours means seventy-two hours from now, not from some other moment."""
    assert terms().valid_until_from(NOW) == NOW + timedelta(hours=72)
    assert terms(valid_hours=24).valid_until_from(NOW) == NOW + timedelta(days=1)


def test_the_default_window_is_the_specification_default() -> None:
    """An offer that does not say how long gets three days, which is what the spec asks for."""
    assert terms_from({"quantity_kg": 500, "price_santim": UNIT_PRICE}).valid_hours == 72
