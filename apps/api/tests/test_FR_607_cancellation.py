"""Cancelling an order before it is paid for, and the kilos that go back.

FR-607 gives the buyer an unconditional right to cancel while an order is still waiting for money,
and gives the seller one only before an order exists. That asymmetry is the whole of this file, and
it is not arbitrary: acceptance is the seller agreeing to sell, so a seller who wants to stop a
deal has an answer for every stage of it - reject the bid, and only once there is an order is a
cancellation the other party's. Refusing the seller is therefore a different answer from refusing a
stranger, and the distinction is visible in the error rather than hidden behind one 403.

The kilos going back in the same transaction is the other half. An order cancelled with its
reservation still held would be stock nobody can buy and nobody can count, and the seller's
listing would quietly be short by exactly the weight of an order that no longer exists.
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

LISTING_KG = 1_000
BUY_KG = 600
#: What is left of a thousand while six hundred are held, and the figure the refused buy-now is
#: told about it.
LEFT_AFTER_BUY_KG = LISTING_KG - BUY_KG


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


def attempt_buy_now(
    client: TestClient,
    headers: dict[str, str],
    listing_id: str,
    body: Callable[..., dict[str, object]],
    key: Callable[[], str],
    **overrides: object,
) -> httpx.Response:
    """Buy now, answering either way - for the tests whose point is the refusal."""
    response = client.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**headers, "Idempotency-Key": key()},
        json=body(**overrides),
    )
    assert isinstance(response, httpx.Response)
    return response


def buy_now(
    client: TestClient,
    headers: dict[str, str],
    listing_id: str,
    body: Callable[..., dict[str, object]],
    key: Callable[[], str],
    **overrides: object,
) -> dict[str, object]:
    """Buy now and hand back the created order."""
    response = attempt_buy_now(client, headers, listing_id, body, key, **overrides)
    assert response.status_code == 201, response.text
    order: dict[str, object] = response.json()
    return order


def cancel(
    client: TestClient,
    headers: dict[str, str],
    order_id: str,
    key: Callable[[], str],
) -> httpx.Response:
    """Cancel an order under a fresh idempotency key."""
    response = client.post(
        f"/v1/orders/{order_id}/cancel", headers={**headers, "Idempotency-Key": key()}
    )
    assert isinstance(response, httpx.Response)
    return response


def test_the_buyer_can_cancel_and_the_kilos_go_back_to_the_listing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """A cancellation, its reason, and a reservation that is no longer holding anything.

    `BUYER_CANCELLED` rather than the funding window, because the two are different facts about
    the future: one is a buyer who chose to stop, the other is a deadline that ran out. Both can
    end an order, and a seller reading their history later should be able to tell which happened
    to each.
    """
    seller = signed_in()
    listing_id = post_listing(
        client, seller, listing_body, quantity_kg=LISTING_KG, min_order_kg=100
    )
    buyer = signed_in("+251911000003")
    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)

    response = cancel(client, buyer, str(order["id"]), idempotency_key)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELLED"
    assert response.json()["cancel_reason"] == "BUYER_CANCELLED"

    stored = stored_orders()[0]
    assert stored["status"] == "CANCELLED"
    assert stored["cancel_reason"] == "BUYER_CANCELLED"

    reservations = stored_reservations()
    assert len(reservations) == 1
    assert reservations[0]["state"] == "RELEASED"

    after = client.get(f"/v1/listings/{listing_id}", headers=seller)
    assert after.json()["available_kg"] == LISTING_KG


def test_the_seller_cannot_cancel_a_deal_they_agreed_to(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """The seller's answer to an order they do not want is no, and they said yes when they took it.

    403 with a reason of its own, because "you are not allowed" would send this seller looking for
    a permission that does not exist. A seller who wants a bid stopped before it becomes an order
    rejects the bid, which is a different word for the same wish and is available at that point.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)

    response = cancel(client, seller, str(order["id"]), idempotency_key)

    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["code"] == "FORBIDDEN"
    assert error["details"]["reason"] == "seller_cannot_cancel_after_acceptance"
    assert stored_orders()[0]["status"] == "AWAITING_FUNDING"
    assert [row["state"] for row in stored_reservations()] == ["HELD"]


def test_a_stranger_cannot_cancel_somebody_elses_order(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """403 with the ordinary reason, which is the one that has nothing to do with the trade.

    The seller and a stranger are both forbidden and their reasons differ, and that difference is
    information about the order the stranger should not have: a stranger's refusal says only that
    they are not a party to it.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)

    response = cancel(client, signed_in("+251911000004"), str(order["id"]), idempotency_key)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["reason"] == "not_a_party_to_this_order"
    assert stored_orders()[0]["status"] == "AWAITING_FUNDING"


def test_cancelling_twice_under_one_key_is_answered_once(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """A second tap under the same key gets the first answer, and the kilos come back once.

    The retry is the ordinary case for a cancellation - the request that answers "cancelled" is the
    one a buyer is most likely to send again because they did not see it - and a release that ran
    twice would be a double refund of stock that only existed once.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)
    key = idempotency_key()

    first = client.post(
        f"/v1/orders/{order['id']}/cancel", headers={**buyer, "Idempotency-Key": key}
    )
    second = client.post(
        f"/v1/orders/{order['id']}/cancel", headers={**buyer, "Idempotency-Key": key}
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert [row["state"] for row in stored_reservations()] == ["RELEASED"]


def test_a_second_cancellation_under_a_fresh_key_is_refused_as_an_illegal_transition(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """A genuinely new request against an already-cancelled order is a state error, not a replay.

    409, from the guard rather than the handler: `CANCELLED` is terminal in §10.4, and the
    cancel_reason it carries is left exactly as the first cancellation wrote it rather than being
    overwritten by a refusal that should have changed nothing.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)
    assert cancel(client, buyer, str(order["id"]), idempotency_key).status_code == 200

    response = cancel(client, buyer, str(order["id"]), idempotency_key)

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["status"] == "CANCELLED"
    assert error["details"]["requested"] == "CANCELLED"
    assert stored_orders()[0]["cancel_reason"] == "BUYER_CANCELLED"


def test_a_cancelled_order_frees_the_listing_for_a_buyer_who_was_turned_away(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The release is not bookkeeping: it is what makes the kilos purchasable again.

    The second buyer was refused because everything was held, and then the first buyer cancelled.
    Their retry under the same terms has to succeed, or the release would be a number in a table
    that nobody could buy from - which is the failure mode FR-1901 exists to prevent.
    """
    seller = signed_in()
    listing_id = post_listing(
        client, seller, listing_body, quantity_kg=LISTING_KG, min_order_kg=100
    )
    first = signed_in("+251911000003")
    second = signed_in("+251911000004")
    order = buy_now(client, first, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)

    refused = attempt_buy_now(
        client, second, listing_id, order_body, idempotency_key, quantity_kg=LISTING_KG
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "VALIDATION_ERROR"
    assert refused.json()["error"]["details"]["available"] == LEFT_AFTER_BUY_KG

    assert cancel(client, first, str(order["id"]), idempotency_key).status_code == 200
    retried = buy_now(
        client, second, listing_id, order_body, idempotency_key, quantity_kg=LISTING_KG
    )

    assert retried["status"] == "AWAITING_FUNDING"
    assert retried["quantity_kg"] == LISTING_KG


def test_a_cancellation_does_not_put_a_sold_out_listing_back_on_sale(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    """§10.1 gives `SOLD_OUT` no edge back to `ACTIVE`, so releasing stock is not re-listing.

    The kilos are sellable again - the listing reports the whole quantity as available - but the
    listing itself stays off sale, because the seller took it down when it emptied and a
    cancellation is not the seller saying otherwise. What a seller does with a listing whose
    buyer walked away is their decision: resume it, or list again. Anything else would be the
    platform putting back on sale a listing somebody had deliberately taken off it.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body, quantity_kg=BUY_KG, min_order_kg=100)
    buyer = signed_in("+251911000003")
    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)
    sold_out = [row for row in stored_listings() if str(row["id"]) == listing_id]
    assert sold_out[0]["status"] == "SOLD_OUT"

    assert cancel(client, buyer, str(order["id"]), idempotency_key).status_code == 200

    after = client.get(f"/v1/listings/{listing_id}", headers=seller)
    assert after.json()["status"] == "SOLD_OUT"
    assert after.json()["available_kg"] == BUY_KG
    assert [row["status"] for row in stored_listings() if str(row["id"]) == listing_id] == [
        "SOLD_OUT"
    ]

    refused = attempt_buy_now(
        client, signed_in("+251911000004"), listing_id, order_body, idempotency_key, quantity_kg=100
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "LISTING_NOT_ACTIVE"


def test_cancelling_an_order_that_is_not_there_is_a_404(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    idempotency_key: Callable[[], str],
) -> None:
    """An identity belonging to nothing answers like any other missing row."""
    buyer = signed_in("+251911000003")

    response = cancel(client, buyer, str(uuid.uuid4()), idempotency_key)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["details"]["resource"] == "order"


def test_a_cancellation_takes_no_body_and_needs_a_key(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """No reason in the body, and a key in the header, because there is nothing to record.

    The reason is `BUYER_CANCELLED` because that is what happened; a body carrying a free-text
    explanation would be a column nobody reads and a place a client could put anything. The key is
    required because a cancellation releases stock, and a buyer's retry of a lost response must not
    release it twice.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)

    no_key = client.post(f"/v1/orders/{order['id']}/cancel", headers=buyer)
    refused_body = client.post(
        f"/v1/orders/{order['id']}/cancel",
        headers={**buyer, "Idempotency-Key": idempotency_key()},
        json={"reason": "found_something_cheaper"},
    )

    assert no_key.status_code == 422, no_key.text
    # Nothing in the body is a route with no body, so an extra key is ignored rather than
    # recorded; what matters is that the cancellation happened and nothing from the body is on
    # the row.
    assert refused_body.status_code == 200, refused_body.text
    stored = stored_orders()[0]
    assert stored["status"] == "CANCELLED"
    assert stored["cancel_reason"] == "BUYER_CANCELLED"
