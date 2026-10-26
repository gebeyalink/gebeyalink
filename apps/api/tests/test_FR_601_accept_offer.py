"""Accepting an offer, which is where an order comes into being.

An acceptance is the seller saying yes to terms a buyer already put in writing, so almost
everything on the order is not a choice this slice gets to make: the weight and the price are the
offer's, the seller is the listing's, and the money is whatever the shared arithmetic says those
two numbers come to. What the request carries is the one thing the platform cannot know - where
the goods are going - and it carries nothing else at all.

What is checked here is that the four rows written - the order, its reservation, the offer's
status and, when it was the last of the stock, the listing's - are one another's consequences and
land together, and that the ones nobody may write are refused by name rather than quietly
ignored.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient

# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

#: The figures the default listing and offer are made of, named so that the money assertions are
#: about the order and not about a subtraction the reader has to do first.
OFFER_KG = 500
OFFER_PRICE_SANTIM = 3_300_000
LISTING_KG = 2_000
#: Two percent of five hundred kilos at the offer's price is far past the ceiling, so this sale
#: exercises the clamp rather than the percentage - which is the branch every real order takes,
#: because any order above twenty-five thousand santim's worth of crop hits the ceiling.
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


def accept(
    client: TestClient,
    headers: dict[str, str],
    offer_id: str,
    body: Callable[..., dict[str, object]],
    key: Callable[[], str],
    **overrides: object,
) -> httpx.Response:
    """Accept an offer under a fresh idempotency key."""
    return cast(
        "httpx.Response",
        client.post(
            f"/v1/offers/{offer_id}/accept",
            headers={**headers, "Idempotency-Key": key()},
            json=body(**overrides),
        ),
    )


def test_accepting_an_offer_creates_an_order_that_holds_the_kilograms(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """One acceptance, and the four things that are true afterwards because of it.

    The order's status is `AWAITING_FUNDING` rather than `CREATED` because the two are written in
    one transaction and the moment the kilos are held is the moment the order is waiting for
    money. An order born `CREATED` would be a state the platform could be shown, which is what the
    state table exists to prevent.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    offer_id = place_offer(
        client, buyer, listing_id, offer_body, quantity_kg=OFFER_KG, price_santim=OFFER_PRICE_SANTIM
    )

    response = accept(client, seller, offer_id, order_body, idempotency_key)

    assert response.status_code == 201, response.text
    order = response.json()
    assert order["listing_id"] == listing_id
    assert order["offer_id"] == offer_id
    assert order["quantity_kg"] == OFFER_KG
    assert order["unit_price_santim"] == OFFER_PRICE_SANTIM
    assert order["status"] == "AWAITING_FUNDING"
    assert order["payment_mode"] == "ESCROW"
    assert order["cancel_reason"] is None
    assert order["buyer_address_text"] == "Adaa, East Shewa"
    assert order["version"] == 1

    assert [row["status"] for row in stored_offers() if str(row["id"]) == offer_id] == ["ACCEPTED"]

    orders = stored_orders()
    assert len(orders) == 1
    assert str(orders[0]["id"]) == order["id"]
    assert str(orders[0]["buyer_id"]) == order["buyer_id"]
    assert str(orders[0]["seller_id"]) == order["seller_id"]

    held = stored_reservations()
    assert len(held) == 1
    assert str(held[0]["listing_id"]) == listing_id
    assert held[0]["holder_type"] == "ORDER"
    assert str(held[0]["holder_id"]) == order["id"]
    assert held[0]["quantity_kg"] == OFFER_KG
    assert held[0]["state"] == "HELD"


def test_acceptance_leaves_the_listing_with_exactly_that_much_less(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Availability moves by the weight of the order and by nothing else.

    FR-1901 makes the listing's quantity the ceiling and every claim on it a reservation row, so
    this is the assertion that the reservation is the real thing rather than a note beside the
    listing: the number the listing reads back is smaller by exactly the kilos accepted.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    offer_id = place_offer(client, buyer, listing_id, offer_body, quantity_kg=OFFER_KG)

    assert accept(client, seller, offer_id, order_body, idempotency_key).status_code == 201

    after = client.get(f"/v1/listings/{listing_id}", headers=buyer)
    assert after.status_code == 200, after.text
    assert after.json()["quantity_kg"] == LISTING_KG
    assert after.json()["available_kg"] == LISTING_KG - OFFER_KG
    assert after.json()["status"] == "ACTIVE"


def test_the_money_on_the_order_is_the_arithmetic_and_not_the_body(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """Every amount comes out of the shared money functions, frozen on the row.

    The fee is at its ceiling here and the VAT is fifteen percent of the ceiling rather than of
    anything else, which is the pair of rules a hand-written total gets wrong most easily: the
    percentage is applied before the clamp, and the VAT is charged on the fee rather than on the
    subtotal.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    offer_id = place_offer(client, buyer, listing_id, offer_body, quantity_kg=OFFER_KG)

    response = accept(client, seller, offer_id, order_body, idempotency_key)

    assert response.status_code == 201, response.text
    order = response.json()
    subtotal = OFFER_KG * OFFER_PRICE_SANTIM
    assert order["delivery_fee_santim"] == 0
    assert order["platform_fee_santim"] == FEE_CEILING_SANTIM
    assert order["vat_santim"] == VAT_ON_CEILING_SANTIM
    # Pickup is the buyer's own journey, so it carries no delivery charge and the total is the
    # subtotal plus the fee and its VAT. Both of those come off the row, not out of the response,
    # so a mismatch here means the stored agreement is not the returned one.
    assert order["total_santim"] == subtotal + FEE_CEILING_SANTIM + VAT_ON_CEILING_SANTIM

    stored = stored_orders()[0]
    assert stored["total_santim"] == order["total_santim"]
    assert stored["platform_fee_santim"] == FEE_CEILING_SANTIM
    assert stored["fee_payer"] == "BUYER"


def test_who_pays_the_fee_is_snapshotted_onto_the_order(
    seller_pays_client: TestClient,
    seller_pays_signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """With the seller paying, the buyer's total is the subtotal and the row says so.

    AC-6.4 asks for the payer to be frozen onto the order rather than read from configuration at
    settlement time, and the only way to see a snapshot is under a policy that would give a
    different answer later. So this runs the same acceptance against an application configured
    with the seller paying, and the row keeps `SELLER` whatever configuration says afterwards.
    """
    seller = seller_pays_signed_in()
    listing_id = post_listing(seller_pays_client, seller, listing_body)
    buyer = seller_pays_signed_in("+251911000003")
    offer_id = place_offer(seller_pays_client, buyer, listing_id, offer_body, quantity_kg=OFFER_KG)

    response = accept(seller_pays_client, seller, offer_id, order_body, idempotency_key)

    assert response.status_code == 201, response.text
    order = response.json()
    subtotal = OFFER_KG * OFFER_PRICE_SANTIM
    assert order["fee_payer"] == "SELLER"
    # The fee is still computed and still stored - it is the platform's revenue either way - but
    # it is not the buyer's total when the seller is the one paying it.
    assert order["platform_fee_santim"] == FEE_CEILING_SANTIM
    assert order["total_santim"] == subtotal
    assert stored_orders()[0]["fee_payer"] == "SELLER"


def test_a_delivery_method_the_listing_does_not_offer_is_refused_by_name(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The default listing is pickup only, so a seller delivery is not a smaller order.

    Refused with the field named and the offer left open, because a bid that does not fit the
    seller's terms is not a bid the buyer has lost: the seller can widen the listing and it stands.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    offer_id = place_offer(client, buyer, listing_id, offer_body)

    response = accept(
        client, seller, offer_id, order_body, idempotency_key, delivery_method="SELLER_DELIVERY"
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "delivery_method" in error["details"]["fields"]
    assert stored_orders() == []


def test_only_the_seller_may_answer_a_bid(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """The buyer cannot accept their own bid, and a stranger cannot accept anybody's.

    Both are 403 rather than 404: the offer is there, the caller simply is not the seller, and
    pretending otherwise would leave a seller unable to tell a mistyped identity from a bid that
    does not exist.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    offer_id = place_offer(client, buyer, listing_id, offer_body)

    from_buyer = accept(client, buyer, offer_id, order_body, idempotency_key)
    stranger = signed_in("+251911000004")
    from_stranger = accept(client, stranger, offer_id, order_body, idempotency_key)

    assert from_buyer.status_code == 403, from_buyer.text
    assert from_buyer.json()["error"]["details"]["reason"] == "not_the_answering_party"
    assert from_stranger.status_code == 403, from_stranger.text
    assert stored_orders() == []
    assert [row["status"] for row in stored_offers()] == ["OPEN"]


def test_an_offer_that_is_no_longer_open_cannot_be_accepted(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """A withdrawn bid is history, and the state table says so rather than the handler.

    409 rather than 422: nothing about the request was malformed, and a client that retries it
    unchanged should be told to stop rather than to fix something.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    offer_id = place_offer(client, buyer, listing_id, offer_body)
    withdrawn = client.post(f"/v1/offers/{offer_id}/withdraw", headers=buyer)
    assert withdrawn.status_code == 200, withdrawn.text

    response = accept(client, seller, offer_id, order_body, idempotency_key)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ILLEGAL_STATE_TRANSITION"
    assert stored_orders() == []


def test_an_acceptance_needs_an_address_and_an_idempotency_key(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The three things the contract insists on, each refused in the way it should be.

    The key is a header and a UUID because that is the contract's shape for it, so a malformed one
    is refused by the framework before any business rule is consulted. The address is required
    because the order's columns are not nullable and a pickup still happens somewhere.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    offer_id = place_offer(client, buyer, listing_id, offer_body)

    no_key = client.post(f"/v1/offers/{offer_id}/accept", headers=seller, json=order_body())
    not_a_uuid = client.post(
        f"/v1/offers/{offer_id}/accept",
        headers={**seller, "Idempotency-Key": "buy-this-once"},
        json=order_body(),
    )
    body = order_body()
    del body["buyer_address_text"]
    no_address = client.post(
        f"/v1/offers/{offer_id}/accept",
        headers={**seller, "Idempotency-Key": idempotency_key()},
        json=body,
    )

    assert no_key.status_code == 422, no_key.text
    assert not_a_uuid.status_code == 422, not_a_uuid.text
    assert no_address.status_code == 422, no_address.text
    assert "buyer_address_text" in no_address.json()["error"]["details"]["fields"]


def test_a_client_cannot_state_the_price_it_was_quoted(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """A body carrying a total is refused by name, which is what `extra="forbid"` buys.

    Without it the field would be ignored and the order would go through at the right price
    anyway - correct by luck, and a client that believed it had negotiated one.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    offer_id = place_offer(client, buyer, listing_id, offer_body)

    response = accept(
        client,
        seller,
        offer_id,
        order_body,
        idempotency_key,
        total_santim=1,
        unit_price_santim=1,
    )

    assert response.status_code == 422, response.text
    fields = response.json()["error"]["details"]["fields"]
    assert "total_santim" in fields
    assert "unit_price_santim" in fields
    assert stored_orders() == []


def test_accepting_an_offer_that_is_not_there_is_a_404(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """An identity that belongs to nothing answers like any other missing row."""
    seller = signed_in()

    response = client.post(
        f"/v1/offers/{uuid.uuid4()}/accept",
        headers={**seller, "Idempotency-Key": idempotency_key()},
        json=order_body(),
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["details"]["resource"] == "offer"


def test_a_paused_listing_answers_no_bids_at_all(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """A paused listing is not for sale, and the bid survives the pause.

    The seller who pauses by accident gets their bid back when they resume, so the offer is left
    `OPEN` rather than spent - the same reasoning as AC-19.1's, that a bid the platform retires on
    the seller's behalf is a bid nobody agreed to retire.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    offer_id = place_offer(client, buyer, listing_id, offer_body)
    paused = client.post(f"/v1/listings/{listing_id}/pause", headers=seller)
    assert paused.status_code == 200, paused.text

    response = accept(client, seller, offer_id, order_body, idempotency_key)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "LISTING_NOT_ACTIVE"
    assert error["details"]["status"] == "PAUSED"
    assert stored_orders() == []
    assert [row["status"] for row in stored_offers()] == ["OPEN"]


def test_the_buyer_may_accept_a_seller_s_own_counter(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """A negotiation ends at whichever row the parties last wrote, whoever wrote it.

    The seller counters, the buyer agrees, and the order is on the counter's terms - not on the
    opening bid's. A rule that only let a seller accept would mean a counter could never be the
    end of anything, and the buyer who asked for those exact terms would have to write them out
    again to get them. The counter's price and weight are what the order records.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    bid = place_offer(
        client, buyer, listing_id, offer_body, quantity_kg=OFFER_KG, price_santim=OFFER_PRICE_SANTIM
    )
    countered = client.post(
        f"/v1/offers/{bid}/counter",
        headers=seller,
        json=offer_body(quantity_kg=OFFER_KG - 100, price_santim=3_350_000),
    )
    assert countered.status_code == 200, countered.text
    counter_id = str(countered.json()["id"])

    response = accept(client, buyer, counter_id, order_body, idempotency_key)

    assert response.status_code == 201, response.text
    order = response.json()
    assert order["offer_id"] == counter_id
    assert order["quantity_kg"] == OFFER_KG - 100
    assert order["unit_price_santim"] == 3_350_000

    statuses = {str(row["id"]): row["status"] for row in stored_offers()}
    assert statuses[counter_id] == "ACCEPTED"
    assert statuses[bid] == "SUPERSEDED"
    assert str(stored_orders()[0]["offer_id"]) == counter_id


def test_neither_party_may_accept_the_terms_they_wrote_themselves(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """The buyer's bid and the seller's counter are both refused, by the same rule.

    A party answering its own terms is not agreeing to them; it is writing a sale and calling it a
    negotiation. The rule is read off which party wrote the row rather than stored per offer, so
    it holds at every depth of a thread - including a counter the buyer wrote after the seller's.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    bid = place_offer(client, buyer, listing_id, offer_body)
    countered = client.post(
        f"/v1/offers/{bid}/counter", headers=seller, json=offer_body(quantity_kg=400)
    )
    assert countered.status_code == 200, countered.text
    counter_id = str(countered.json()["id"])
    buyer_recounter = client.post(
        f"/v1/offers/{counter_id}/counter", headers=buyer, json=offer_body(quantity_kg=300)
    )
    assert buyer_recounter.status_code == 200, buyer_recounter.text
    deepest = str(buyer_recounter.json()["id"])

    own_bid = accept(client, buyer, bid, order_body, idempotency_key)
    own_counter = accept(client, seller, counter_id, order_body, idempotency_key)
    own_reanswer = accept(client, buyer, deepest, order_body, idempotency_key)

    for response in (own_bid, own_counter, own_reanswer):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["details"]["reason"] == "not_the_answering_party"
    assert stored_orders() == []
    # Ordered by depth, so this reads as the thread did: the buyer's bid superseded by the
    # seller's counter, which the buyer then superseded with a bid of their own.
    assert [row["status"] for row in stored_offers()] == ["SUPERSEDED", "SUPERSEDED", "OPEN"]


def test_fr_505_accepting_rejects_only_the_offers_that_no_longer_fit(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_offers: Callable[[], list[dict[str, object]]],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """FR-505: accepting an offer rejects all other OPEN offers on that listing if
    remaining quantity < their quantity (partial-quantity offers remain open)."""
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body, quantity_kg=100)

    buyer_a = signed_in("+251911000003")
    buyer_b = signed_in("+251911000004")
    buyer_c = signed_in("+251911000005")
    buyer_d = signed_in("+251911000006")

    offer_a = place_offer(client, buyer_a, listing_id, offer_body, quantity_kg=60)
    offer_b = place_offer(client, buyer_b, listing_id, offer_body, quantity_kg=30)
    offer_c = place_offer(client, buyer_c, listing_id, offer_body, quantity_kg=10)
    offer_d = place_offer(client, buyer_d, listing_id, offer_body, quantity_kg=50)

    response = accept(client, seller, offer_a, order_body, idempotency_key)
    assert response.status_code == 201, response.text
    order = response.json()
    assert order["quantity_kg"] == 60

    by_id = {str(row["id"]): row for row in stored_offers()}
    assert by_id[offer_a]["status"] == "ACCEPTED"
    assert by_id[offer_b]["status"] == "OPEN"  # 30 <= 40 remaining
    assert by_id[offer_c]["status"] == "OPEN"  # 10 <= 40 remaining
    assert by_id[offer_d]["status"] == "REJECTED"  # 50 > 40 remaining
    assert len(stored_orders()) == 1
