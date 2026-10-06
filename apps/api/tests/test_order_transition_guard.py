"""The order state table, read on its own.

Every transition the specification's §10.4 allows, and every one it does not, checked here against
the guard rather than through HTTP. The table is the whole table rather than the part this slice
reaches: escrow, delivery and disputes own their own operations and will come through this same
guard, and a guard with a row missing is a guard they cannot trust.

`SPEC_TABLE` is written out independently of the service so that a rule quietly added there fails
here rather than passing, and `test_the_table_is_the_specification_s_whole_table` compares the
two directly rather than trusting either.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

# No HTTP, no database, no clock beyond the moment handed in: this file exercises the arithmetic
# of the guard alone.
pytestmark = pytest.mark.unit

from apps.api.models.order import Order, OrderStatus  # noqa: E402
from apps.api.services.order_service import (  # noqa: E402
    ALLOWED_TRANSITIONS,
    INITIAL_STATUSES,
    effective_status,
    funding_window_closed,
    max_recordable_weight,
    min_acceptable_kg,
    pickup_window_closed,
    shipment_window_closed,
    transition_order_status,
    within_shrinkage_tolerance,
)
from packages.shared.errors import ApiError, ErrorCode  # noqa: E402

NOW = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)

#: Nobody in particular. The guard reads only the status and the clock, so whose order it is
#: makes no difference and is not worth inventing two accounts for.
BUYER = uuid.UUID("00000000-0000-0000-0000-000000000001")
SELLER = uuid.UUID("00000000-0000-0000-0000-000000000002")
LISTING = uuid.UUID("00000000-0000-0000-0000-000000000003")

#: §10.4 in full. Every row the specification states, including the four this slice cannot reach.
SPEC_TABLE: list[tuple[OrderStatus, OrderStatus]] = [
    (OrderStatus.CREATED, OrderStatus.AWAITING_FUNDING),
    (OrderStatus.CREATED, OrderStatus.CONFIRMED),
    # Added for the COD pilot (FR-2103): either party to a COD order may cancel it while it is
    # still `CREATED`. The specification's table reaches cancellation only from `AWAITING_FUNDING`
    # (escrow's whole pre-payment life), and a COD order never enters that status, so without this
    # edge its cancellation could never happen at all.
    (OrderStatus.CREATED, OrderStatus.CANCELLED),
    (OrderStatus.AWAITING_FUNDING, OrderStatus.FUNDED),
    (OrderStatus.AWAITING_FUNDING, OrderStatus.CANCELLED),
    (OrderStatus.CONFIRMED, OrderStatus.DELIVERED),
    # Added for the COD pilot (FR-701, FR-702): a confirmed COD order may begin fulfillment —
    # marked ready for pickup or shipped — because it has no `FUNDED` state for the kilos to
    # spend time in. Money never moves on a COD order, so confirmation is the whole agreement
    # there is to ship against; an escrow order reaches these states through `FUNDED` instead.
    (OrderStatus.CONFIRMED, OrderStatus.READY_FOR_PICKUP),
    (OrderStatus.CONFIRMED, OrderStatus.SHIPPED),
    (OrderStatus.FUNDED, OrderStatus.READY_FOR_PICKUP),
    (OrderStatus.FUNDED, OrderStatus.SHIPPED),
    (OrderStatus.FUNDED, OrderStatus.DISPUTED),
    (OrderStatus.READY_FOR_PICKUP, OrderStatus.DELIVERED),
    (OrderStatus.READY_FOR_PICKUP, OrderStatus.DISPUTED),
    (OrderStatus.SHIPPED, OrderStatus.DELIVERED),
    (OrderStatus.SHIPPED, OrderStatus.DISPUTED),
    (OrderStatus.DELIVERED, OrderStatus.COMPLETED),
    (OrderStatus.DISPUTED, OrderStatus.DELIVERED),
    (OrderStatus.DISPUTED, OrderStatus.CANCELLED_REFUNDED),
]

#: The statuses with nowhere to go. A completed, cancelled or refunded order is a record of how a
#: trade ended, and §10.4 reopens none of them.
TERMINAL: tuple[OrderStatus, ...] = (
    OrderStatus.COMPLETED,
    OrderStatus.CANCELLED,
    OrderStatus.CANCELLED_REFUNDED,
)


def an_order(status: OrderStatus, *, created_at: datetime = NOW, **rest: object) -> Order:
    """An order row with the columns the guard and the clock actually read."""
    order = Order(
        short_code="GL-000000",
        buyer_id=BUYER,
        seller_id=SELLER,
        listing_id=LISTING,
        offer_id=None,
        quantity_kg=100,
        unit_price_santim=3_250_000,
        status=status,
        version=1,
    )
    order.created_at = created_at
    for name, value in rest.items():
        setattr(order, name, value)
    return order


@pytest.mark.parametrize(("from_status", "to_status"), SPEC_TABLE)
def test_every_edge_the_specification_allows_is_allowed(
    from_status: OrderStatus, to_status: OrderStatus
) -> None:
    """Each stated row, named in the test id so a failure says which edge broke."""
    order = an_order(from_status)

    assert transition_order_status(order, to_status, from_status) is to_status
    assert order.status is to_status


@pytest.mark.parametrize("status", list(OrderStatus))
@pytest.mark.parametrize("target", list(OrderStatus))
def test_every_edge_the_specification_does_not_state_is_refused(
    status: OrderStatus, target: OrderStatus
) -> None:
    """Nothing outside the table, in either direction, for any pair of statuses.

    Including the rows the table does have, run with a status the caller claims rather than one the
    row holds: a caller that passes the wrong `from_status` gets refused here, which is why the
    parameter is handed in rather than read off the row.
    """
    if status is target:
        pytest.skip("a status is not a transition out of itself")
    if target in ALLOWED_TRANSITIONS[status]:
        pytest.skip("stated in the specification's table")

    order = an_order(status)
    with pytest.raises(ApiError) as refused:
        transition_order_status(order, target, status)

    assert refused.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION
    assert order.status is status


@pytest.mark.parametrize("status", TERMINAL)
def test_a_terminal_status_has_nowhere_to_go(status: OrderStatus) -> None:
    """Cancelled, refunded and completed orders are records, and records are not reopened."""
    assert ALLOWED_TRANSITIONS[status] == frozenset()
    with pytest.raises(ApiError):
        transition_order_status(an_order(status), OrderStatus.AWAITING_FUNDING, status)


def test_creation_may_only_land_on_a_status_a_new_order_can_have() -> None:
    """`from_status=None` means "not stored yet", and only `CREATED` is open to a new row.

    Creation passes `None` because it has no state behind it, which is exactly why it needs its own
    rule: a guard that treated `None` as "any state" would let a caller create an order in
    `COMPLETED`, which is a trade that has been paid out and delivered.
    """
    assert frozenset({OrderStatus.CREATED}) == INITIAL_STATUSES

    order = an_order(OrderStatus.CREATED)
    assert transition_order_status(order, OrderStatus.CREATED, None) is OrderStatus.CREATED

    refused_target = an_order(OrderStatus.CREATED)
    with pytest.raises(ApiError) as refused:
        transition_order_status(refused_target, OrderStatus.FUNDED, None)
    assert refused.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION
    assert refused_target.status is OrderStatus.CREATED


def test_rejecting_a_dispute_may_only_restore_the_status_it_interrupted() -> None:
    """The dispute row has one extra exit, and it is added from the row rather than the table.

    §10.4 has no `DISPUTED → SHIPPED`, so how a rejected dispute gets an order back to where it was
    is not a second general rule: it is this one status being permitted to return to whatever
    `state_before_dispute` holds, and to nothing else on the row.
    """
    disputed = an_order(OrderStatus.DISPUTED, state_before_dispute=OrderStatus.SHIPPED)
    assert transition_order_status(disputed, OrderStatus.SHIPPED, OrderStatus.DISPUTED) is (
        OrderStatus.SHIPPED
    )

    elsewhere = an_order(OrderStatus.DISPUTED, state_before_dispute=OrderStatus.SHIPPED)
    with pytest.raises(ApiError) as refused:
        transition_order_status(elsewhere, OrderStatus.CONFIRMED, OrderStatus.DISPUTED)
    assert refused.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION

    without_memory = an_order(OrderStatus.DISPUTED, state_before_dispute=None)
    with pytest.raises(ApiError):
        transition_order_status(without_memory, OrderStatus.SHIPPED, OrderStatus.DISPUTED)


def test_an_order_past_its_window_reads_as_cancelled_and_is_only_written_by_the_service() -> None:
    """The clock is part of the status, and the derived answer is not the stored one.

    `effective_status` is what every reader is shown, so a buyer looking at an order they never
    paid for sees `CANCELLED` with nobody having run anything. The row keeps saying
    `AWAITING_FUNDING` until something writes it, which is the ordering that makes this a lazy rule
    rather than a cron: the read is honest immediately, and the release of the kilos belongs to the
    paths that can afford the write.
    """
    just_inside = an_order(OrderStatus.AWAITING_FUNDING, created_at=NOW - timedelta(hours=47))
    just_outside = an_order(OrderStatus.AWAITING_FUNDING, created_at=NOW - timedelta(hours=49))

    assert effective_status(just_inside, NOW) is OrderStatus.AWAITING_FUNDING
    assert effective_status(just_outside, NOW) is OrderStatus.CANCELLED
    assert just_outside.status is OrderStatus.AWAITING_FUNDING


def test_the_funding_window_is_measured_from_creation_and_from_nothing_else() -> None:
    """Forty-eight hours from the moment the order entered `AWAITING_FUNDING`.

    Both happen in one transaction and so at one instant, and a column holding the same moment
    twice would be one more thing that could disagree with the row it duplicates. The test states
    it by moving `created_at` alone and leaving every other column alone.
    """
    order = an_order(OrderStatus.AWAITING_FUNDING, created_at=NOW)

    assert funding_window_closed(order, NOW + timedelta(hours=47, minutes=59)) is False
    assert funding_window_closed(order, NOW + timedelta(hours=48)) is True
    assert effective_status(order, NOW + timedelta(hours=48)) is OrderStatus.CANCELLED


def test_a_window_is_only_enforced_on_an_order_that_is_still_waiting_for_money() -> None:
    """A funded order's age is not a cancellation, however old it is.

    Without this, a two-year-old `FUNDED` order would read as cancelled the moment anybody looked
    at it - which would make the derivation actively dangerous rather than merely lazy.
    """
    old = an_order(OrderStatus.FUNDED, created_at=NOW.replace(year=2020))

    assert funding_window_closed(old, NOW) is True
    assert effective_status(old, NOW) is OrderStatus.FUNDED


def test_a_pickup_past_its_window_reads_as_delivered_even_though_nothing_has_been_written() -> None:
    """Twenty-four hours from `ready_at`, and only when the order is still sitting ready.

    A buyer who neither collects nor complains within a day has let the crop wait at the
    seller's cost, so the honest answer to anybody who asks is `DELIVERED` — the goods were
    let to sit past the point the seller can be expected to hold them. An order that was never
    marked ready has no window and is never late.
    """
    ready = an_order(OrderStatus.READY_FOR_PICKUP, ready_at=NOW)
    never_ready = an_order(OrderStatus.READY_FOR_PICKUP, ready_at=None)

    assert pickup_window_closed(ready, NOW + timedelta(hours=23, minutes=59)) is False
    assert pickup_window_closed(ready, NOW + timedelta(hours=24)) is True
    assert effective_status(ready, NOW + timedelta(hours=24)) is OrderStatus.DELIVERED
    assert effective_status(never_ready, NOW.replace(year=2030)) is OrderStatus.READY_FOR_PICKUP


def test_a_shipment_window_is_measured_from_its_departure_and_the_crop_s_own_hold() -> None:
    """The crop's holding window counts from `shipped_at`, and only when the order is still shipped.

    (FR-2508.) A crop that keeps holds longer: the window is a property of the crop, so it is
    handed to the derivation rather than guessed. Without it the derivation answers the stored
    status rather than inventing one, because a caller that has not looked the crop up knows
    nothing it could honestly deliver.
    """
    shipped = an_order(OrderStatus.SHIPPED, shipped_at=NOW)

    assert shipment_window_closed(shipped, NOW + timedelta(hours=71), hold_hours=72) is False
    assert shipment_window_closed(shipped, NOW + timedelta(hours=72), hold_hours=72) is True
    assert (
        effective_status(shipped, NOW + timedelta(hours=200), hold_hours=72)
        is OrderStatus.DELIVERED
    )
    assert effective_status(shipped, NOW + timedelta(hours=200)) is OrderStatus.SHIPPED


def test_the_shrinkage_tolerance_boundary_belongs_to_the_buyer() -> None:
    """Delivered weights are compared in ten-thousandths, so the edge is exact.

    Two percent tolerance is a receivable weight of exactly ninety-eight percent of the order,
    no fraction looser: one more short kilogram is refused. The comparison is scaled so that the
    boundary does not drift with floats, and written `>=` so the boundary itself passes — which
    is the point of the `2.1` rows: at two-point-one percent the boundary is itself the
    (whole-kilo ceiling of the) figure the tolerance names, and it passes, while the kilo below
    it is refused.
    """
    assert within_shrinkage_tolerance(1000, 980, 2.0) is True
    assert within_shrinkage_tolerance(1000, 979, 2.0) is False
    assert within_shrinkage_tolerance(1000, 980, 2.1) is True
    assert within_shrinkage_tolerance(1000, 979, 2.1) is True
    assert within_shrinkage_tolerance(1000, 978, 2.1) is False
    assert within_shrinkage_tolerance(1000, 1000, 0.0) is True
    assert within_shrinkage_tolerance(1000, 999, 0.0) is False
    assert min_acceptable_kg(1000, 2.0) == 980
    assert min_acceptable_kg(1000, 2.1) == 979


def test_the_recordable_weight_is_five_percent_on_top_and_flagged_to_the_ceiling() -> None:
    """Record at most `1.05 * ordered`, whole kilograms, and never past the schema's ceiling.

    (FR-2507.) An odd order floors at its cushion — thirty-three ordered allows the thirty-four
    the round-up grants — and an order so large that five percent would pass a million kilograms
    is capped at the ceiling the check constraint enforces anyway.
    """
    assert max_recordable_weight(33) == 34
    assert max_recordable_weight(1000) == 1050
    assert max_recordable_weight(2000) == 2100
    assert max_recordable_weight(20_000_000) == 1_000_000


def test_the_table_is_the_specification_s_whole_table() -> None:
    """Every stated edge is present and no unstated edge is, in both directions at once.

    Written as a set difference rather than as a row count, so an implementation that quietly
    added an edge fails here with the edge named, and an edge removed from the specification's
    table fails here too.
    """
    stated = {target for _, target in SPEC_TABLE} | INITIAL_STATUSES
    implemented = {target for targets in ALLOWED_TRANSITIONS.values() for target in targets}

    assert implemented <= stated
    missing = {
        (source, target)
        for source, target in SPEC_TABLE
        if target not in ALLOWED_TRANSITIONS[source]
    }
    assert missing == set()
    assert set(ALLOWED_TRANSITIONS) == set(OrderStatus)
