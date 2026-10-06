"""Buying now, which is the same transaction as an acceptance with nothing to negotiate.

The offer's absence is what makes this path different, and it is worth noticing what that leaves
behind: no offer row to check, no agreed price to read, and no second party to be satisfied. The
weight is the buyer's and the price is the listing's, so every bound that an acceptance gets from
the offer has to be got from somewhere else here - the listing's own minimum, the sum of what is
left, and the fact that the seller's asking price is not negotiable and so cannot be sent by a
client trying to negotiate it.

What is checked below is that those bounds are enforced in the same order and answered in the same
way as an acceptance's, because a buyer who discovers two different sets of rules depending on
which button they pressed has been told the wrong thing about the platform.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

# SPEC A20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

#: The default listing's ask, kept as a name because the money assertions are about where a number
#: came from rather than about its size.
ASK_SANTIM = 3_250_000
LISTING_KG = 2_000
BUY_KG = 500
#: Two percent of this subtotal is past the ceiling, so the fee is clamped and the VAT is fifteen
#: percent of the ceiling. The same two figures the acceptance tests use, for the same reason.
FEE_CEILING_SANTIM = 50_000
VAT_ON_CEILING_SANTIM = 7_500


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


def buy_now(
    client: TestClient,
    headers: dict[str, str],
    listing_id: str,
    body: Callable[..., dict[str, object]],
    key: Callable[[], str],
    **overrides: object,
) -> httpx.Response:
    """Buy now under a fresh idempotency key."""
    response = client.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**headers, "Idempotency-Key": key()},
        json=body(**overrides),
    )
    assert isinstance(response, httpx.Response)
    return response


def test_buying_now_takes_the_listing_price_and_holds_the_kilos(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """One buy-now, and everything about it read back from the rows rather than the response.

    `offer_id` is null because nothing was negotiated: there is no bid behind this order, and a
    column pointing at one would be a fiction the rest of the platform would have to special-case.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")

    response = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)

    assert response.status_code == 201, response.text
    order = response.json()
    assert order["listing_id"] == listing_id
    assert order["offer_id"] is None
    assert order["quantity_kg"] == BUY_KG
    assert order["unit_price_santim"] == ASK_SANTIM
    assert order["status"] == "AWAITING_FUNDING"
    assert order["fee_payer"] == "BUYER"

    subtotal = BUY_KG * ASK_SANTIM
    assert order["platform_fee_santim"] == FEE_CEILING_SANTIM
    assert order["vat_santim"] == VAT_ON_CEILING_SANTIM
    assert order["total_santim"] == subtotal + FEE_CEILING_SANTIM + VAT_ON_CEILING_SANTIM

    stored = stored_orders()[0]
    assert stored["unit_price_santim"] == ASK_SANTIM
    assert stored["offer_id"] is None
    assert str(stored["seller_id"]) == order["seller_id"]

    held = stored_reservations()
    assert len(held) == 1
    assert held[0]["holder_type"] == "ORDER"
    assert str(held[0]["holder_id"]) == order["id"]
    assert held[0]["quantity_kg"] == BUY_KG
    assert held[0]["state"] == "HELD"

    after = client.get(f"/v1/listings/{listing_id}", headers=buyer)
    assert after.json()["available_kg"] == LISTING_KG - BUY_KG


def test_the_seller_cannot_buy_their_own_listing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """A seller moving their own stock onto their own order would move nothing.

    Refused with the listing named, and refused before the weight is even looked at: there is no
    quantity that makes this a sale. No order and no reservation, because a failed request that
    had written half its rows would be a bug this test exists to prevent.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)

    response = buy_now(client, seller, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"] == {"listing_id": "you_cannot_buy_your_own_listing"}
    assert stored_orders() == []
    assert stored_reservations() == []


def test_buying_less_than_the_listing_s_own_minimum_is_refused_by_name(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """A seller who will not sell a hundred kilos is not obliged to sell fifty.

    The minimum is the listing's rather than a platform constant, and the refusal says which
    listing's, because a buyer looking at three listings needs to know which one this is about.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body, min_order_kg=100)
    buyer = signed_in("+251911000003")

    response = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=99)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"] == {"quantity_kg": "must_be_at_least_listing_min_order_kg"}
    assert error["details"]["min_order_kg"] == 100
    assert stored_orders() == []


def test_buying_more_than_is_left_is_refused_with_the_available_amount(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """Most of the listing, then more of it than is left.

    422 with the available amount rather than a bare refusal, for the same reason an acceptance's
    oversize bid gets one: a buyer who has been told there are a hundred kilos left can buy a
    hundred kilos straight away, and one told only "unavailable" can only guess.

    The listing is deliberately left partly unsold, because a listing with nothing left is a
    different refusal - `LISTING_NOT_ACTIVE` - and reaching both through one arrangement would
    only prove that whichever check comes first fires.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body, quantity_kg=1_000, min_order_kg=50)
    first = signed_in("+251911000003")
    second = signed_in("+251911000004")
    first_order = buy_now(client, first, listing_id, order_body, idempotency_key, quantity_kg=900)
    assert first_order.status_code == 201, first_order.text

    response = buy_now(client, second, listing_id, order_body, idempotency_key, quantity_kg=150)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"] == {"quantity_kg": "exceeds_available_quantity"}
    assert error["details"]["available"] == 100
    assert len(stored_orders()) == 1
    assert len(stored_reservations()) == 1


def test_a_seller_delivery_charges_the_fee_the_listing_names(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The listing offers seller delivery at a fee, and choosing it charges that fee.

    Read from the listing rather than from the buyer, because the buyer does not get to decide
    what delivery costs - and the total follows from it, since an order whose total ignored the
    delivery fee would be asking the buyer to pay for a journey they had not been quoted.
    """
    seller = signed_in()
    listing_id = post_listing(
        client,
        seller,
        listing_body,
        delivery_options=["PICKUP", "SELLER_DELIVERY"],
        delivery_fee_santim=150_000,
    )
    buyer = signed_in("+251911000003")

    response = buy_now(
        client,
        buyer,
        listing_id,
        order_body,
        idempotency_key,
        quantity_kg=BUY_KG,
        delivery_method="SELLER_DELIVERY",
    )

    assert response.status_code == 201, response.text
    order = response.json()
    assert order["delivery_method"] == "SELLER_DELIVERY"
    assert order["delivery_fee_santim"] == 150_000
    assert order["total_santim"] == BUY_KG * ASK_SANTIM + 150_000 + FEE_CEILING_SANTIM + (
        VAT_ON_CEILING_SANTIM
    )
    pickup = buy_now(
        client,
        signed_in("+251911000004"),
        listing_id,
        order_body,
        idempotency_key,
        quantity_kg=BUY_KG,
    )
    assert pickup.json()["delivery_fee_santim"] == 0
    fees = {str(row["id"]): row["delivery_fee_santim"] for row in stored_orders()}
    assert fees[order["id"]] == 150_000


def test_buying_the_last_kilos_sells_the_listing_out_and_stops_further_sales(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    """The transition is written by the sale, not left to a job that does not exist.

    A listing that has just sold its last kilo and still reads as active would take another bid it
    cannot honour, so `SOLD_OUT` is part of the same transaction as the reservation - and the next
    buyer is told the listing is not on sale rather than being invited into a refusal about stock.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body, quantity_kg=BUY_KG)
    buyer = signed_in("+251911000003")

    bought = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)

    assert bought.status_code == 201, bought.text
    stored = [row for row in stored_listings() if str(row["id"]) == listing_id]
    assert stored[0]["status"] == "SOLD_OUT"

    later = buy_now(
        client,
        signed_in("+251911000004"),
        listing_id,
        order_body,
        idempotency_key,
        quantity_kg=BUY_KG,
    )
    assert later.status_code == 422, later.text
    error = later.json()["error"]
    assert error["code"] == "LISTING_NOT_ACTIVE"
    assert error["details"]["status"] == "SOLD_OUT"


def test_a_paused_listing_cannot_be_bought_from(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """Pause is the seller saying the listing is not for sale, and buy-now is a sale.

    The same refusal as an acceptance gets, and for the same reason: the listing service's rule
    for what a listing's status is is the one that decides, so a listing cannot be purchasable
    through one route and not through the other.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    paused = client.post(f"/v1/listings/{listing_id}/pause", headers=seller)
    assert paused.status_code == 200, paused.text

    response = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "LISTING_NOT_ACTIVE"
    assert stored_orders() == []


def test_buying_a_listing_that_is_not_there_is_a_404(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """A missing listing is answered before the lock is taken, and answers like any other 404."""
    buyer = signed_in("+251911000003")

    response = buy_now(
        client, buyer, str(uuid.uuid4()), order_body, idempotency_key, quantity_kg=BUY_KG
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["details"]["resource"] == "listing"


def test_a_buyer_cannot_state_the_price_they_were_quoted(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """Buy-now is the route for taking the asking price, and `extra="forbid"` keeps it that way.

    A body carrying a price is refused by name rather than ignored: a client that believed it had
    negotiated would have been told it was right and left with an order at the listing's price.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")

    response = buy_now(
        client,
        buyer,
        listing_id,
        order_body,
        idempotency_key,
        quantity_kg=BUY_KG,
        unit_price_santim=1,
        total_santim=1,
    )

    assert response.status_code == 422, response.text
    fields = response.json()["error"]["details"]["fields"]
    assert "unit_price_santim" in fields
    assert "total_santim" in fields
    assert stored_orders() == []


def test_buying_needs_a_weight_and_an_idempotency_key(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The two things the contract insists on, refused in the way each should be.

    Without a weight there is nothing to sell and the listing's total is not an answer, so it is
    required rather than defaulted: a buy-now for two thousand kilos that silently meant the whole
    listing would be the most expensive mis-tap in the product.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")

    no_key = client.post(
        f"/v1/listings/{listing_id}/buy-now", headers=buyer, json=order_body(quantity_kg=BUY_KG)
    )
    body = order_body()
    assert body.pop("quantity_kg", None) is None
    no_weight = client.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**buyer, "Idempotency-Key": idempotency_key()},
        json=body,
    )

    assert no_key.status_code == 422, no_key.text
    assert no_weight.status_code == 422, no_weight.text
    assert "quantity_kg" in no_weight.json()["error"]["details"]["fields"]
