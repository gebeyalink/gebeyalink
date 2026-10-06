"""The forty-eight hours, and what happens to a listing's kilos when nobody uses them.

FR-603 is the one rule in this slice whose enforcement is a write. An order still waiting for
money past its window is cancelled, and a cancelled order's reservation is released, so the kilos
become sellable again. Getting that wrong does not merely leave a stale status on a row: it holds
a seller's stock against an order nobody is going to pay for, which is stock the seller cannot
count, cannot list again and cannot explain.

There is no worker here. The window is enforced by whoever looks at an order, so the two paths
that can lose a sale are the ones that matter - the acceptance and buy-now sweeps, which run
before availability is counted, and the read, which is where a buyer looking at their own order
finds out what has happened to it. `GET /orders/mine` deliberately does not write, and there is a
test below that says so, because a list is a page of history and a write behind a scroll would
put a transaction on the path of a scroll gesture.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

# SPEC A20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

LISTING_KG = 1_000
SOLD_KG = 800
#: What is left of a thousand after eight hundred are held, and what the next bid is measured
#: against once the lapsed hold has been swept out of the way.
LEFT_AFTER_SALE_KG = LISTING_KG - SOLD_KG


def post_listing(
    client: TestClient,
    headers: dict[str, str],
    body: Callable[..., dict[str, object]],
    **overrides: object,
) -> str:
    """Post a listing and hand back its identity."""
    response = client.post("/v1/listings", headers=headers, json=body(**overrides))
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def place_offer(
    client: TestClient,
    headers: dict[str, str],
    listing_id: str,
    body: Callable[..., dict[str, object]],
    **overrides: object,
) -> str:
    """Place an offer and hand back its identity."""
    response = client.post(
        f"/v1/listings/{listing_id}/offers", headers=headers, json=body(**overrides)
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def buy_now(
    client: TestClient,
    headers: dict[str, str],
    listing_id: str,
    body: Callable[..., dict[str, object]],
    key: Callable[[], str],
    **overrides: object,
) -> dict[str, object]:
    """Buy now and hand back the created order."""
    response = client.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**headers, "Idempotency-Key": key()},
        json=body(**overrides),
    )
    assert response.status_code == 201, response.text
    order: dict[str, object] = response.json()
    return order


def test_reading_an_order_past_its_window_cancels_it_and_gives_the_kilos_back(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    lapse_order: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """A read is also a write, and the reason it is written here is the reservation.

    The buyer opens an order they never paid for and finds it cancelled. Underneath that, the
    kilos are back with the seller - which is the part that makes this a read-with-a-write rather
    than a read that reports a derived status: an order that read as cancelled while its
    reservation was still held would be lying in two directions at once, to the buyer and to
    whoever counts the seller's stock.
    """
    seller = signed_in()
    listing_id = post_listing(
        client, seller, listing_body, quantity_kg=LISTING_KG, min_order_kg=100
    )
    buyer = signed_in("+251911000003")
    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=SOLD_KG)
    lapse_order(str(order["id"]))

    response = client.get(f"/v1/orders/{order['id']}", headers=buyer)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELLED"
    assert response.json()["cancel_reason"] == "FUNDING_EXPIRED"

    stored = stored_orders()[0]
    assert stored["status"] == "CANCELLED"
    assert stored["cancel_reason"] == "FUNDING_EXPIRED"
    reservations = stored_reservations()
    assert len(reservations) == 1
    assert reservations[0]["state"] == "RELEASED"

    after = client.get(f"/v1/listings/{listing_id}", headers=buyer)
    assert after.json()["available_kg"] == LISTING_KG
    # Released stock does not bring a listing back to sale: §10.1 gives `SOLD_OUT` no edge to
    # `ACTIVE`, and this listing was never sold out, so it is still active and now whole.
    assert after.json()["status"] == "ACTIVE"


def test_a_lapsed_order_reads_as_cancelled_in_a_list_without_giving_the_kilos_back(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    lapse_order: Callable[[str], None],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """The list is honest about the status and writes nothing, and both halves are checked.

    Derived-on-read means the buyer sees `CANCELLED` here without a worker having run, which is
    what makes the lazy rule work at all. Not writing means a page of history costs no
    transaction and the reservation is still held: the next sale, or the next read of the order
    itself, is what sweeps it.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=SOLD_KG)
    lapse_order(str(order["id"]))

    listed = client.get("/v1/orders/mine", headers=buyer, params={"as": "buyer"})

    assert listed.status_code == 200, listed.text
    page = listed.json()
    assert [row["id"] for row in page["data"]] == [order["id"]]
    assert page["data"][0]["status"] == "CANCELLED"

    reservations = stored_reservations()
    assert [row["state"] for row in reservations] == ["HELD"]


def test_the_next_sale_sweeps_a_lapsed_hold_out_of_the_way_before_it_counts(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    lapse_order: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """A hold nobody is going to pay for must not be able to fail a live bid.

    Eight hundred kilos are held by a buyer who has gone quiet, and a second buyer bids for the
    whole thousand. Counting availability before sweeping would refuse that bid with
    "two hundred left", which would be a lie about stock that is on the shelf and for sale - so
    the sweep runs first, the bid is accepted, and the lapsed order is cancelled in the same
    transaction.
    """
    seller = signed_in()
    listing_id = post_listing(
        client, seller, listing_body, quantity_kg=LISTING_KG, min_order_kg=100
    )
    quiet = signed_in("+251911000003")
    lapsed = buy_now(client, quiet, listing_id, order_body, idempotency_key, quantity_kg=SOLD_KG)
    lapse_order(str(lapsed["id"]))

    second = signed_in("+251911000004")
    offer_id = place_offer(
        client, second, listing_id, offer_body, quantity_kg=LISTING_KG, price_santim=3_300_000
    )
    accepted = client.post(
        f"/v1/offers/{offer_id}/accept",
        headers={**seller, "Idempotency-Key": idempotency_key()},
        json=order_body(),
    )

    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["quantity_kg"] == LISTING_KG

    orders = {str(row["id"]): row for row in stored_orders()}
    assert orders[str(lapsed["id"])]["status"] == "CANCELLED"
    assert orders[str(lapsed["id"])]["cancel_reason"] == "FUNDING_EXPIRED"
    assert orders[accepted.json()["id"]]["status"] == "AWAITING_FUNDING"

    states = {str(row["holder_id"]): row["state"] for row in stored_reservations()}
    assert states[str(lapsed["id"])] == "RELEASED"
    assert states[accepted.json()["id"]] == "HELD"

    listing = client.get(f"/v1/listings/{listing_id}", headers=seller)
    assert listing.json()["available_kg"] == 0
    assert listing.json()["status"] == "SOLD_OUT"


def test_an_order_inside_its_window_is_left_alone(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """The everyday case, asserted so that the expiry tests cannot pass by cancelling everything.

    An order created an instant ago is `AWAITING_FUNDING`, its reason is empty and its kilos are
    held. Without this, a sweep that fired on every read would pass every other test in the file
    and cost a real buyer their order.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=SOLD_KG)

    response = client.get(f"/v1/orders/{order['id']}", headers=buyer)

    assert response.json()["status"] == "AWAITING_FUNDING"
    assert response.json()["cancel_reason"] is None
    assert stored_orders()[0]["status"] == "AWAITING_FUNDING"
    assert [row["state"] for row in stored_reservations()] == ["HELD"]


def test_an_order_that_has_expired_cannot_be_cancelled_again_by_its_buyer(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    lapse_order: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The window closed, so the buyer's own cancellation has nothing left to do.

    409 rather than a cheerful 200: the buyer asked to cancel an order that is already cancelled,
    and telling them it worked would leave them believing they ended something that had ended on
    its own. The refusal is judged against the clock and writes nothing, so the row still says
    `AWAITING_FUNDING` with no reason - which is not the buyer having cancelled it, and not the
    platform having quietly taken the row's word for anything. The next read of the order is what
    writes the reason down, and it writes the funding window rather than the buyer.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=SOLD_KG)
    lapse_order(str(order["id"]))

    cancelled = client.post(
        f"/v1/orders/{order['id']}/cancel",
        headers={**buyer, "Idempotency-Key": idempotency_key()},
    )

    assert cancelled.status_code == 409, cancelled.text
    error = cancelled.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["status"] == "CANCELLED"
    untouched = stored_orders()[0]
    assert untouched["status"] == "AWAITING_FUNDING"
    assert untouched["cancel_reason"] is None

    read = client.get(f"/v1/orders/{order['id']}", headers=buyer)
    assert read.json()["cancel_reason"] == "FUNDING_EXPIRED"
    assert stored_orders()[0]["cancel_reason"] == "FUNDING_EXPIRED"
