"""A seller's edit, and the one edit that would sell stock somebody has already bought.

FR-1904 puts the enforcement in the same transaction that writes the listing, and that placement
is the requirement rather than an implementation detail. Availability lives in the reservation
rows, so "how much is really there" is a sum over another table, and an edit judged against a
read taken outside the listing's lock would be judged against a quantity a sale on the other side
of the database has already changed. The sale paths take the same lock for the same reason, which
is why an edit and an acceptance cannot both decide what the last kilo is.

The refusal names the orders holding the kilos rather than only reporting a number, because "there
are 800 kilos committed" is not actionable on its own: a seller with four orders has to work out
which of them, and the four are right there.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient

# SPEC A20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

LISTING_KG = 1_000
FIRST_BUY_KG = 400
SECOND_BUY_KG = 300
#: What the two orders between them are holding, and the figure the refusal reports.
COMMITTED_KG = FIRST_BUY_KG + SECOND_BUY_KG


def post_listing(
    client: TestClient,
    headers: dict[str, str],
    body: Callable[..., dict[str, object]],
    **overrides: object,
) -> dict[str, object]:
    """Post a listing and hand back what was created."""
    response = client.post("/v1/listings", headers=headers, json=body(**overrides))
    assert response.status_code == 201, response.text
    listing: dict[str, object] = response.json()
    return listing


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


def edit(
    client: TestClient,
    headers: dict[str, str],
    listing: dict[str, object],
    **changes: object,
) -> httpx.Response:
    """Edit a listing at the version its last response carried."""
    response = client.patch(
        f"/v1/listings/{listing['id']}",
        headers=headers,
        json={"version": listing["version"], **changes},
    )
    assert isinstance(response, httpx.Response)
    return response


def test_a_listing_cannot_be_cut_below_what_an_order_is_holding(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    """The refusal, the figure it reports, and the listing left exactly as it was.

    Not "the edit was rejected" but the row's own `quantity_kg` still reading a thousand: a refusal
    that half-applied would leave a seller believing they have stock they do not have, which is the
    one outcome FR-1904 exists to prevent.
    """
    seller = signed_in()
    listing = post_listing(client, seller, listing_body, quantity_kg=LISTING_KG, min_order_kg=100)
    order = buy_now(
        client,
        signed_in("+251911000003"),
        str(listing["id"]),
        order_body,
        idempotency_key,
        quantity_kg=FIRST_BUY_KG,
    )

    response = edit(client, seller, listing, quantity_kg=FIRST_BUY_KG - 1)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "ORDER_CONFLICT"
    details = error["details"]
    assert details["requested_quantity_kg"] == FIRST_BUY_KG - 1
    assert details["committed_kg"] == FIRST_BUY_KG
    assert [held["order_id"] for held in details["blocking_orders"]] == [order["id"]]
    assert details["blocking_orders"][0]["short_code"] == order["short_code"]
    assert details["blocking_orders"][0]["quantity_kg"] == FIRST_BUY_KG
    assert details["blocking_orders"][0]["state"] == "HELD"
    assert [row["quantity_kg"] for row in stored_listings()] == [LISTING_KG]


def test_a_listing_may_be_cut_to_exactly_what_is_held(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    """The invariant is `available >= 0`, not `available > 0`, so exactly held is allowed.

    A seller who has sold four hundred kilos and then sets the listing's quantity to what is left
    has made the listing an empty shell rather than an over-promise, and the rule says that is
    fine. What the seller reads back is `SOLD_OUT`, derived from the zero availability rather than
    written by this edit: FR-306's trigger reaching zero through an edit instead of through a
    sale, with the stored column still `ACTIVE` underneath it.
    """
    seller = signed_in()
    listing = post_listing(client, seller, listing_body, quantity_kg=LISTING_KG, min_order_kg=100)
    buy_now(
        client,
        signed_in("+251911000003"),
        str(listing["id"]),
        order_body,
        idempotency_key,
        quantity_kg=FIRST_BUY_KG,
    )

    response = edit(client, seller, listing, quantity_kg=FIRST_BUY_KG)

    assert response.status_code == 200, response.text
    assert response.json()["quantity_kg"] == FIRST_BUY_KG
    assert response.json()["available_kg"] == 0
    assert response.json()["status"] == "SOLD_OUT"
    assert [row["status"] for row in stored_listings()] == ["ACTIVE"]


def test_the_refusal_names_every_order_that_is_holding_stock(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Two buyers, one edit, and both orders named with their short codes.

    The short codes are the point rather than the order ids: a seller reading a phone is going to
    look for `GL-` codes, and an order id is what they would have to cross-reference to find one.
    """
    seller = signed_in()
    listing = post_listing(client, seller, listing_body, quantity_kg=LISTING_KG, min_order_kg=100)
    first = buy_now(
        client,
        signed_in("+251911000003"),
        str(listing["id"]),
        order_body,
        idempotency_key,
        quantity_kg=FIRST_BUY_KG,
    )
    second = buy_now(
        client,
        signed_in("+251911000004"),
        str(listing["id"]),
        order_body,
        idempotency_key,
        quantity_kg=SECOND_BUY_KG,
    )

    response = edit(client, seller, listing, quantity_kg=COMMITTED_KG - 1)

    assert response.status_code == 422, response.text
    details = response.json()["error"]["details"]
    assert details["committed_kg"] == COMMITTED_KG
    named = {held["short_code"] for held in details["blocking_orders"]}
    assert named == {first["short_code"], second["short_code"]}
    assert sum(held["quantity_kg"] for held in details["blocking_orders"]) == COMMITTED_KG


def test_the_price_and_the_rest_are_still_editable_while_stock_is_held(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Only the quantity is guarded, and an order is not frozen just because stock is held.

    A seller who has sold four hundred kilos and then decides their price was wrong must still be
    able to say so; refusing it would mean the first sale locks the seller into their first price
    for as long as the order exists. The order keeps the price it agreed at, which is the same
    reason the fee payer is snapshotted onto the order.
    """
    seller = signed_in()
    listing = post_listing(client, seller, listing_body, quantity_kg=LISTING_KG, min_order_kg=100)
    buy_now(
        client,
        signed_in("+251911000003"),
        str(listing["id"]),
        order_body,
        idempotency_key,
        quantity_kg=FIRST_BUY_KG,
    )

    response = edit(
        client,
        seller,
        listing,
        unit_price_santim=3_400_000,
        description="Repriced after a first sale",
    )

    assert response.status_code == 200, response.text
    assert response.json()["unit_price_santim"] == 3_400_000
    assert response.json()["available_kg"] == LISTING_KG - FIRST_BUY_KG


def test_raising_the_quantity_is_never_refused(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """More stock cannot breach an invariant about stock, so the guard does not look at it.

    A seller who has found another two thousand kilos has not created a problem for anybody, and
    being told they may not add to their own listing would be a strange refusal to have to explain.
    """
    seller = signed_in()
    listing = post_listing(client, seller, listing_body, quantity_kg=LISTING_KG, min_order_kg=100)
    buy_now(
        client,
        signed_in("+251911000003"),
        str(listing["id"]),
        order_body,
        idempotency_key,
        quantity_kg=FIRST_BUY_KG,
    )

    response = edit(client, seller, listing, quantity_kg=LISTING_KG + 2_000)

    assert response.status_code == 200, response.text
    assert response.json()["available_kg"] == LISTING_KG + 2_000 - FIRST_BUY_KG


def test_a_cancelled_order_stops_blocking_the_edit_that_would_have_breached_it(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The refusal tracks the reservations, not the orders that once existed.

    This is what `RELEASED` is in the state machine for: a buyer's change of mind releases the
    kilos, and a seller who then cuts their listing to what they really have left must not be told
    by a rule about a hold that no longer holds anything. Guarding on orders instead of
    reservations would refuse this edit for as long as the cancelled order stayed on file.
    """
    seller = signed_in()
    listing = post_listing(client, seller, listing_body, quantity_kg=LISTING_KG, min_order_kg=100)
    order = buy_now(
        client,
        signed_in("+251911000003"),
        str(listing["id"]),
        order_body,
        idempotency_key,
        quantity_kg=FIRST_BUY_KG,
    )
    refused = edit(client, seller, listing, quantity_kg=FIRST_BUY_KG - 1)
    assert refused.status_code == 422, refused.text

    cancelled = client.post(
        f"/v1/orders/{order['id']}/cancel",
        headers={**signed_in("+251911000003"), "Idempotency-Key": idempotency_key()},
    )
    assert cancelled.status_code == 200, cancelled.text

    allowed = edit(client, seller, listing, quantity_kg=FIRST_BUY_KG - 1)

    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["quantity_kg"] == FIRST_BUY_KG - 1


def test_an_open_bid_does_not_block_the_edit_and_fails_its_own_acceptance(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """AC-3.3: an offer reserves nothing, so it is not an edit's business.

    The seller may cut the listing under an unanswered bid, and the bid is left `OPEN` rather than
    retired on the seller's behalf. What happens next is the seller's refusal arriving where it
    belongs - at the acceptance, by arithmetic, with the available amount - rather than at the
    moment the listing was edited, which would have been the platform rejecting a bid on the
    seller's behalf before the seller had seen it.
    """
    seller = signed_in()
    listing = post_listing(client, seller, listing_body, quantity_kg=LISTING_KG, min_order_kg=100)
    buyer = signed_in("+251911000003")
    offered = client.post(
        f"/v1/listings/{listing['id']}/offers",
        headers=buyer,
        json=offer_body(quantity_kg=LISTING_KG, price_santim=3_300_000),
    )
    assert offered.status_code == 201, offered.text
    offer_id = str(offered.json()["id"])

    allowed = edit(client, seller, listing, quantity_kg=FIRST_BUY_KG)

    assert allowed.status_code == 200, allowed.text
    assert [row["status"] for row in stored_offers()] == ["OPEN"]

    refused = client.post(
        f"/v1/offers/{offer_id}/accept",
        headers={**seller, "Idempotency-Key": idempotency_key()},
        json=order_body(),
    )

    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "OFFER_CONFLICT"
    assert error["details"]["available"] == FIRST_BUY_KG
    assert error["details"]["requested_kg"] == LISTING_KG


def test_an_edit_at_the_wrong_version_is_refused_with_the_current_row(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Two phones holding the same listing, and the loser is handed what it needs to retry.

    The version is checked before the stock guard, so a stale edit is told it is stale rather than
    being told about committed kilos it had not seen. The current row comes back in the response
    because a caller told only that it lost cannot do anything with the loss.
    """
    seller = signed_in()
    listing = post_listing(client, seller, listing_body)

    first = client.patch(
        f"/v1/listings/{listing['id']}",
        headers=seller,
        json={"version": listing["version"], "description": "First phone"},
    )
    stale = client.patch(
        f"/v1/listings/{listing['id']}",
        headers=seller,
        json={"version": listing["version"], "description": "Second phone"},
    )

    assert first.status_code == 200, first.text
    assert stale.status_code == 409, stale.text
    details = stale.json()["error"]["details"]
    assert details["expected_version"] == listing["version"]
    assert details["current"]["version"] == cast("int", listing["version"]) + 1
    assert details["current"]["description"] == "First phone"


def test_a_stranger_cannot_edit_a_listing_however_much_stock_it_holds(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    """Ownership is checked before the stock rule, so the refusal is about the caller.

    A stranger is not told how much of somebody else's listing is committed, which would be a
    small leak about a business, and is not given the chance to discover an order exists either.
    """
    seller = signed_in()
    listing = post_listing(client, seller, listing_body, quantity_kg=LISTING_KG, min_order_kg=100)
    buy_now(
        client,
        signed_in("+251911000003"),
        str(listing["id"]),
        order_body,
        idempotency_key,
        quantity_kg=FIRST_BUY_KG,
    )

    response = edit(client, signed_in("+251911000004"), listing, quantity_kg=100)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert [row["quantity_kg"] for row in stored_listings()] == [LISTING_KG]
