"""The crop changing hands: ready, shipped, and the code that proves the pickup happened.

FR-701, FR-702 and FR-703 are the fulfillment of a COD order. The seller marks a pickup ready
and a delivery shipped — two standalone messages, because neither is the delivery itself — and
the delivery is counted by the two people who meet for it: the seller breaks the buyer's pickup
code at a pickup (FR-703), and the buyer confirms a shipped order arrived (FR-702). Consuming
the reservation and stamping `delivered_at` happen as one committed step with the status, so
there is no moment where the goods changed hands and the platform still thinks they are held.

The route bodies are what the delivery method demands. `Idempotency-Key` is required on all five
fulfillment routes because each could do its work twice: a second settled delivery is stock
consumed twice, and the guard alone cannot tell a settled delivery from a settlement to be made
— both a ready pickup and a delivered one permit `DELIVERED`, so each confirmation pins the
precondition the design actually means.

Everything here runs against the COD-enabled application, because that is the loop whose UI will
serve the handover; an escrow order is fulfilled through the funding slice in a later task, and
the test below only checks that this slice refuses one politely.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

# SPEC A20: this file drives the real app against an ephemeral Postgres through a client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

CHEAP_LISTING_KG = 2_000
CHEAP_ASK_SANTIM = 1_000
CHEAP_DELIVERY_FEE_SANTIM = 150_000
COD_BUY_KG = 200

SIX_DIGITS = r"^[0-9]{6}$"


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


def confirm(client: TestClient, headers: dict[str, str], order_id: str) -> dict[str, object]:
    """Confirm an order and hand back the read."""
    response = client.post(f"/v1/orders/{order_id}/confirm", headers=headers)
    assert response.status_code == 200, response.text
    order: dict[str, object] = response.json()
    return order


def attempt(
    client: TestClient,
    headers: dict[str, str],
    path: str,
    key: Callable[[], str],
    *,
    json: dict[str, Any] | None = None,
) -> httpx.Response:
    """Call a fulfillment route, answering either way - for the tests whose point is the refusal."""
    request: dict[str, object] = {}
    if json is not None:
        request["json"] = json
    response = client.post(path, headers={**headers, "Idempotency-Key": key()}, **request)
    assert isinstance(response, httpx.Response)
    return response


def ready(
    client: TestClient, headers: dict[str, str], order_id: str, key: Callable[[], str]
) -> dict[str, object]:
    """Mark an order ready for pickup and hand back the read."""
    response = attempt(client, headers, f"/v1/orders/{order_id}/ready", key)
    assert response.status_code == 200, response.text
    order: dict[str, object] = response.json()
    return order


def shipped(
    client: TestClient, headers: dict[str, str], order_id: str, key: Callable[[], str]
) -> dict[str, object]:
    """Mark an order shipped and hand back the read."""
    response = attempt(client, headers, f"/v1/orders/{order_id}/shipped", key)
    assert response.status_code == 200, response.text
    order: dict[str, object] = response.json()
    return order


def pickup_confirm(
    client: TestClient,
    headers: dict[str, str],
    order_id: str,
    key: Callable[[], str],
    code: str,
) -> dict[str, object]:
    """Break the pickup code and hand back the read."""
    response = attempt(
        client, headers, f"/v1/orders/{order_id}/confirm-pickup", key, json={"pickup_code": code}
    )
    assert response.status_code == 200, response.text
    order: dict[str, object] = response.json()
    return order


def delivery_confirm(
    client: TestClient, headers: dict[str, str], order_id: str, key: Callable[[], str]
) -> dict[str, object]:
    """Confirm a shipped delivery and hand back the read."""
    response = attempt(client, headers, f"/v1/orders/{order_id}/confirm-delivery", key)
    assert response.status_code == 200, response.text
    order: dict[str, object] = response.json()
    return order


def pickup_order(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    **order_overrides: object,
) -> tuple[str, str]:
    """Stand up a validated COD pickup order and hand back its identity and listing."""
    listing_id = post_listing(
        client,
        seller,
        listing_body,
        quantity_kg=CHEAP_LISTING_KG,
        unit_price_santim=CHEAP_ASK_SANTIM,
    )
    order = buy_now(
        client,
        buyer,
        listing_id,
        order_body,
        idempotency_key,
        payment_mode="COD",
        quantity_kg=COD_BUY_KG,
        **order_overrides,
    )
    return str(order["id"]), listing_id


def delivery_order(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> tuple[str, str]:
    """Stand up a validated COD seller-delivery order and hand back its identity and listing."""
    listing_id = post_listing(
        client,
        seller,
        listing_body,
        quantity_kg=CHEAP_LISTING_KG,
        unit_price_santim=CHEAP_ASK_SANTIM,
        delivery_options=["SELLER_DELIVERY"],
        delivery_fee_santim=CHEAP_DELIVERY_FEE_SANTIM,
    )
    order = buy_now(
        client,
        buyer,
        listing_id,
        order_body,
        idempotency_key,
        payment_mode="COD",
        quantity_kg=COD_BUY_KG,
        delivery_method="SELLER_DELIVERY",
    )
    return str(order["id"]), listing_id


def pickup_code_of(client: TestClient, headers: dict[str, str], order_id: str) -> str:
    """The buyer's copy of the code, read from the API the way the app itself would."""
    read = client.get(f"/v1/orders/{order_id}", headers=headers).json()
    code = read["pickup_code"]
    assert code is not None, read
    return str(code)


def test_a_pickup_order_walks_from_ready_to_a_code_confirmed_delivery(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """Ready, then the code, then `DELIVERED` - with the kilos consumed at the same moment.

    The food grains handover has two ends and the seller touches both: the buyer's code is the
    proof that the person presenting it holds the buyer's side of the order, so the seller breaks
    it at the handover and the delivery is counted where it happened. The status, the stamp and
    the consumed reservation commit as one step, so the platform never thinks it is still holding
    kilos somebody has already carried away.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = pickup_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    code = pickup_code_of(cod_client, buyer, order_id)

    marked = ready(cod_client, seller, order_id, idempotency_key)

    assert marked["status"] == "READY_FOR_PICKUP"
    assert marked["ready_at"] is not None
    assert set(marked) >= {
        "ready_at",
        "delivered_at",
        "delivered_weight_kg",
        "pickup_code",
    }

    delivered = pickup_confirm(cod_client, seller, order_id, idempotency_key, code)

    assert delivered["status"] == "DELIVERED"
    assert delivered["delivered_at"] is not None
    assert delivered["pickup_code"] is None

    row = stored_orders()[0]
    assert row["status"] == "DELIVERED"
    assert row["ready_at"] is not None
    assert row["delivered_at"] is not None
    assert row["pickup_code"] is None
    assert stored_reservations()[0]["state"] == "CONSUMED"

    buyers_view = cod_client.get(f"/v1/orders/{order_id}", headers=buyer).json()
    assert buyers_view["status"] == "DELIVERED"
    assert buyers_view["pickup_code"] is None


def test_the_seller_s_copy_of_an_order_never_carries_the_pickup_code(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The code is the proof against the seller, so the seller is never shown it.

    A code the party being verified can see is a code the verification proves nothing about. The
    buyer's copy of a fresh order carries it, the row itself carries it, and the seller's copy of
    the same order shows `None` — the seller's handset never bears the secret it is supposed to
    witness.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = pickup_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)

    sellers_view = cod_client.get(f"/v1/orders/{order_id}", headers=seller).json()
    buyers_view = cod_client.get(f"/v1/orders/{order_id}", headers=buyer).json()
    row = stored_orders()[0]

    assert sellers_view["pickup_code"] is None
    assert sellers_view["delivery_method"] == "PICKUP"
    code = buyers_view["pickup_code"]
    assert code is not None
    assert re.match(SIX_DIGITS, code)
    assert row["pickup_code"] == code


def test_a_seller_delivery_walks_from_shipped_to_a_buyer_confirmed_delivery(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """Shipped to the buyer, delivered by the buyer's own word.

    The seller sends the crop and the buyer is the one who sees it arrive, so the buyer's
    confirmation is the delivery's other boundary — the one person in the trade the shortfall
    would hurt is the one whose signature closes it.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = delivery_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key
    )
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)

    sent = shipped(cod_client, seller, order_id, idempotency_key)

    assert sent["status"] == "SHIPPED"
    assert sent["shipped_at"] is not None

    delivered = delivery_confirm(cod_client, buyer, order_id, idempotency_key)

    assert delivered["status"] == "DELIVERED"
    assert delivered["delivered_at"] is not None
    assert stored_reservations()[0]["state"] == "CONSUMED"


def test_a_fulfillment_message_refuses_a_method_that_has_no_such_thing(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Ready names the pickup, shipped names the delivery, and each refuses the other's.

    The wrong method gets the 409 that names it: a pickup has no shipment, a delivery has no
    readiness, and the state table allows the move exactly when the method is the one being asked
    about. The two obligations refuse on different lines — the method on one, the state on the
    other — so the answer can say which of the two was wrong.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()

    pickup_id, _ = pickup_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key
    )
    confirm(cod_client, buyer, pickup_id)
    confirm(cod_client, seller, pickup_id)
    ship_refusal = attempt(cod_client, seller, f"/v1/orders/{pickup_id}/shipped", idempotency_key)
    assert ship_refusal.status_code == 409, ship_refusal.text
    error = ship_refusal.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["requested"] == "SHIPPED"
    assert error["details"]["delivery_method"] == "PICKUP"

    delivery_id, _ = delivery_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key
    )
    confirm(cod_client, buyer, delivery_id)
    confirm(cod_client, seller, delivery_id)
    ready_refusal = attempt(cod_client, seller, f"/v1/orders/{delivery_id}/ready", idempotency_key)
    assert ready_refusal.status_code == 409, ready_refusal.text
    error = ready_refusal.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["requested"] == "READY_FOR_PICKUP"
    assert error["details"]["delivery_method"] == "SELLER_DELIVERY"


def test_only_the_seller_marks_and_collects_and_only_the_buyer_confirms_arrival(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    cod_sign_in: Callable[..., httpx.Response],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The fulfillment of an order is its two parties' business, and 403 is the whole of who.

    The buyer has no order of theirs to mark ready, the seller has no delivery of theirs to sign
    off, and a stranger has no standing at either. Each refusal names the side that was asked to
    do the other's job.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = pickup_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)

    buyer_marks = attempt(cod_client, buyer, f"/v1/orders/{order_id}/ready", idempotency_key)
    assert buyer_marks.status_code == 403, buyer_marks.text
    assert buyer_marks.json()["error"]["details"]["reason"] == "not_the_seller"

    shipped_id, _ = delivery_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key
    )
    confirm(cod_client, buyer, shipped_id)
    confirm(cod_client, seller, shipped_id)
    shipped(cod_client, seller, shipped_id, idempotency_key)

    seller_confirms = attempt(
        cod_client, seller, f"/v1/orders/{shipped_id}/confirm-delivery", idempotency_key
    )
    assert seller_confirms.status_code == 403, seller_confirms.text
    assert seller_confirms.json()["error"]["details"]["reason"] == "not_the_buyer"

    verified = cod_sign_in("+251911000002")
    stranger = {"Authorization": f"Bearer {verified.json()['access_token']}"}
    code = pickup_code_of(cod_client, buyer, order_id)
    stranger_collects = attempt(
        cod_client,
        stranger,
        f"/v1/orders/{order_id}/confirm-pickup",
        idempotency_key,
        json={"pickup_code": code},
    )
    assert stranger_collects.status_code == 403, stranger_collects.text
    assert stranger_collects.json()["error"]["details"]["reason"] == "not_the_seller"


def test_a_wrong_pickup_code_stops_the_settlement_and_the_order_is_untouched(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """A wrong secret blocks the delivery rather than partially proceeding.

    The code is a six-digit secret shared between the two parties of this order, so the check is
    exact. A wrong code stops the settlement before the reservation is consumed, and the order
    still reads `READY_FOR_PICKUP` afterwards — the right code works on the second try, because
    the first try changed nothing.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = pickup_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    code = pickup_code_of(cod_client, buyer, order_id)
    ready(cod_client, seller, order_id, idempotency_key)

    wrong = attempt(
        cod_client,
        seller,
        f"/v1/orders/{order_id}/confirm-pickup",
        idempotency_key,
        json={"pickup_code": "000000" if code != "000000" else "111111"},
    )

    assert wrong.status_code == 422, wrong.text
    error = wrong.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"]["pickup_code"] == "does_not_match"
    assert stored_reservations()[0]["state"] == "CONVERTED"

    still_waiting = cod_client.get(f"/v1/orders/{order_id}", headers=seller).json()
    assert still_waiting["status"] == "READY_FOR_PICKUP"

    delivered = pickup_confirm(cod_client, seller, order_id, idempotency_key, code)
    assert delivered["status"] == "DELIVERED"


def test_a_handover_cannot_be_counted_before_there_is_one_to_count(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Confirmations pin their precondition, because the guard cannot tell the two apart.

    A ready pickup and a delivered one both permit `DELIVERED`, so an order still `CONFIRMED` is
    refused by name - `status` says what it actually is - rather than drifting into being
    counted. The same refusal pins the buyer's confirmation to a shipment in transit.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = pickup_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)

    too_early = attempt(
        cod_client,
        seller,
        f"/v1/orders/{order_id}/confirm-pickup",
        idempotency_key,
        json={"pickup_code": "123456"},
    )
    assert too_early.status_code == 409, too_early.text
    error = too_early.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["status"] == "CONFIRMED"
    assert error["details"]["requested"] == "DELIVERED"

    untouched = cod_client.get(f"/v1/orders/{order_id}", headers=seller).json()
    assert untouched["status"] == "CONFIRMED"
    assert untouched["ready_at"] is None

    shipped_id, _ = delivery_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key
    )
    confirm(cod_client, buyer, shipped_id)
    confirm(cod_client, seller, shipped_id)
    early_delivery = attempt(
        cod_client, buyer, f"/v1/orders/{shipped_id}/confirm-delivery", idempotency_key
    )
    assert early_delivery.status_code == 409, early_delivery.text
    error = early_delivery.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["status"] == "CONFIRMED"
    assert error["details"]["requested"] == "DELIVERED"


def test_a_seller_cannot_mark_ready_an_order_the_guard_has_not_confirmed(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Readiness waits for the whole agreement, whether or not the seller signed first.

    A half-signed COD order is still `CREATED` — one party cannot hold the other to a word
    nobody seconded — and the guard gives `CREATED` no edge to `READY_FOR_PICKUP`. Readiness is
    owed against a confirmed agreement only.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = pickup_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    confirm(cod_client, buyer, order_id)

    response = attempt(cod_client, seller, f"/v1/orders/{order_id}/ready", idempotency_key)

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["status"] == "CREATED"
    assert error["details"]["requested"] == "READY_FOR_PICKUP"


def test_a_second_ready_under_the_same_key_replays_and_a_new_key_refuses(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """A retry replays the first answer; a genuinely new attempt collides with the state.

    The header is what makes the second tap not a second mutation: same key, same answer. A
    different key is the seller insisting on a new readiness for an order that is already
    `READY_FOR_PICKUP`, which the guard refuses because the state has nowhere to go from itself.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = pickup_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    key = str(idempotency_key())

    first = client_post(cod_client, seller, f"/v1/orders/{order_id}/ready", key)
    assert first.status_code == 200, first.text
    replay = client_post(cod_client, seller, f"/v1/orders/{order_id}/ready", key)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()

    again = attempt(cod_client, seller, f"/v1/orders/{order_id}/ready", idempotency_key)
    assert again.status_code == 409, again.text
    error = again.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["status"] == "READY_FOR_PICKUP"
    assert error["details"]["requested"] == "READY_FOR_PICKUP"


def client_post(client: TestClient, headers: dict[str, str], path: str, key: str) -> httpx.Response:
    """POST a route under an explicit (already-chosen) key."""
    response = client.post(path, headers={**headers, "Idempotency-Key": key})
    assert isinstance(response, httpx.Response)
    return response


def test_an_escrow_order_refuses_this_slice_s_fulfillment(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Escrow is fulfilled through its own slice, later; nobody jumps the queue.

    An escrow order's readiness is a question for the money-holding slice that owns its move to
    `FUNDED`. Before money arrives the order is `AWAITING_FUNDING`, which has no readiness, and
    the guard says so with the same 409 it gives any order that cannot go where it is asked.
    """
    seller = signed_in()
    buyer = signed_in_as_buyer()
    listing_id = post_listing(client, seller, listing_body)
    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=COD_BUY_KG)

    response = attempt(client, seller, f"/v1/orders/{order['id']}/ready", idempotency_key)

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["status"] == "AWAITING_FUNDING"
    assert error["details"]["requested"] == "READY_FOR_PICKUP"


def test_a_pickup_that_waits_a_day_is_deemed_collected_by_the_read(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    backdate_ready: Callable[[str], None],
    stored_reservations: Callable[[], list[dict[str, object]]],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """Twenty-four hours of nobody, and the honest answer is `DELIVERED`.

    A buyer who neither collects nor complains within a day has let the crop wait at the
    seller's cost, so the read answers `DELIVERED` — and because the read may be the only thing
    that ever runs, the read is the write: the kilos are consumed and the code is spent in the
    same lazy step, exactly the way a lapsed funding window cancels an unpaid order.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = pickup_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    ready(cod_client, seller, order_id, idempotency_key)

    backdate_ready(order_id)
    read = cod_client.get(f"/v1/orders/{order_id}", headers=buyer).json()

    assert read["status"] == "DELIVERED"
    assert read["delivered_at"] is not None
    assert read["pickup_code"] is None
    assert stored_reservations()[0]["state"] == "CONSUMED"
    assert stored_orders()[0]["status"] == "DELIVERED"


def test_a_shipment_whose_holding_window_has_passed_is_deemed_received_by_the_read(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    backdate_shipped: Callable[[str], None],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """A crop's holding window is measured from shipment, and the read enforces it (FR-2508).

    The kilos are on the move from `shipped_at`, so the wait is counted from then; once the
    crop's given hours are past the order reads as received and the reservation is consumed —
    the money and the kilos are moved by design, not by a request that may never come.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = delivery_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key
    )
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    shipped(cod_client, seller, order_id, idempotency_key)

    backdate_shipped(order_id)
    read = cod_client.get(f"/v1/orders/{order_id}", headers=buyer).json()

    assert read["status"] == "DELIVERED"
    assert read["delivered_at"] is not None
    assert stored_reservations()[0]["state"] == "CONSUMED"
