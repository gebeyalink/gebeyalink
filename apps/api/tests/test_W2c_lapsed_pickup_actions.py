"""The three actions that write a pickup, against one the worker has not settled yet.

W2b moved the pickup window's write to the worker's sweep, and taught the read to derive
`DELIVERED` for an order whose window has closed while the row still says `READY_FOR_PICKUP`.
The tests W2b left prove that derivation. This file proves the consequence for the three routes
that could otherwise write over the same order: confirming the pickup, cancelling, and recording
a weight all read the order through `effective_status`, see `DELIVERED`, and refuse.

Nothing here trusts the response's own account of what it did. The claim is not that a route
answered a refusal; it is that nothing was written, so every assertion reads PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

# SPEC A20: this file drives the real app against an ephemeral Postgres, so it is integration.
pytestmark = pytest.mark.integration

CHEAP_LISTING_KG = 2_000
CHEAP_ASK_SANTIM = 1_000
COD_BUY_KG = 200
#: A weight inside the five-percent cushion, so any refusal below is the state's, not the range's.
RECORDED_WEIGHT_KG = 200
#: The derived status the three actions should see, and the refusal they should get for it.
LAPSED_STATUS = "DELIVERED"
REFUSAL_STATUS = 409
REFUSAL_CODE = "ILLEGAL_STATE_TRANSITION"


def post_listing(
    client: TestClient,
    headers: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    **overrides: object,
) -> str:
    """List a pickup crop and hand back its identity."""
    response = client.post("/v1/listings", headers=headers, json=listing_body(**overrides))
    assert response.status_code == 201, response.text
    listing: dict[str, object] = response.json()
    return str(listing["id"])


def buy_now(
    client: TestClient,
    headers: dict[str, str],
    listing_id: str,
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    *,
    payment_mode: str = "COD",
    quantity_kg: int = COD_BUY_KG,
) -> dict[str, object]:
    """Buy the listing at the asking price and hand back the read."""
    response = client.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**headers, "Idempotency-Key": idempotency_key()},
        json=order_body(payment_mode=payment_mode, quantity_kg=quantity_kg),
    )
    assert response.status_code == 201, response.text
    order: dict[str, object] = response.json()
    return order


def confirm(client: TestClient, headers: dict[str, str], order_id: str) -> None:
    """Confirm the COD agreement."""
    response = client.post(f"/v1/orders/{order_id}/confirm", headers=headers)
    assert response.status_code == 200, response.text


def ready(
    client: TestClient, headers: dict[str, str], order_id: str, key: Callable[[], str]
) -> None:
    """Mark the pickup ready, which starts the window this file lapses."""
    response = client.post(
        f"/v1/orders/{order_id}/ready",
        headers={**headers, "Idempotency-Key": key()},
    )
    assert response.status_code == 200, response.text


def attempt(
    client: TestClient,
    headers: dict[str, str],
    method: str,
    path: str,
    key: Callable[[], str],
    *,
    json: dict[str, Any] | None = None,
) -> httpx.Response:
    """Call one of the three routes, answering either way rather than asserting success."""
    request_headers = {**headers, "Idempotency-Key": key()}
    if json is None:
        response = client.request(method, path, headers=request_headers)
    else:
        response = client.request(method, path, headers=request_headers, json=json)
    assert isinstance(response, httpx.Response)
    return response


def lapsed_unpersisted_pickup(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    backdate_ready: Callable[[str], None],
) -> str:
    """A ready COD pickup whose window has closed, with the row the worker has not touched.

    Both parties confirm, the seller says the goods are waiting, and readiness is pushed past the
    twenty-four-hour window behind the API's back. The row still says `READY_FOR_PICKUP` because
    only the read derives the delivered state and the sweep has not run - which is exactly the
    order the three actions below are aimed at.
    """
    listing_id = post_listing(
        client,
        seller,
        listing_body,
        quantity_kg=CHEAP_LISTING_KG,
        unit_price_santim=CHEAP_ASK_SANTIM,
    )
    order_id = str(buy_now(client, buyer, listing_id, order_body, idempotency_key)["id"])
    confirm(client, buyer, order_id)
    confirm(client, seller, order_id)
    ready(client, seller, order_id, idempotency_key)
    backdate_ready(order_id)
    return order_id


def assert_refused(response: httpx.Response, requested: str) -> None:
    """The route said no because the derived state said delivered first."""
    assert response.status_code == REFUSAL_STATUS, response.text
    error = response.json()["error"]
    assert error["code"] == REFUSAL_CODE
    assert error["details"]["status"] == LAPSED_STATUS
    assert error["details"]["requested"] == requested


def assert_the_row_is_untouched(
    order_id: str,
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """Nothing wrote: the row still waits for the worker and the kilos are still held."""
    row = next(stored for stored in stored_orders() if str(stored["id"]) == order_id)
    assert row["status"] == "READY_FOR_PICKUP"
    assert row["delivered_at"] is None
    assert row["pickup_code"] is not None
    assert row["delivered_via"] is None
    assert row["cancel_reason"] is None
    assert row["delivered_weight_kg"] is None
    reservation = next(
        stored for stored in stored_reservations() if str(stored["holder_id"]) == order_id
    )
    assert reservation["state"] == "CONVERTED"


def test_confirm_pickup_refuses_a_lapsed_unpersisted_pickup(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    backdate_ready: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """The seller breaks the right code, and the state refuses before the code is compared."""
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id = lapsed_unpersisted_pickup(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key, backdate_ready
    )
    code = str(stored_orders()[0]["pickup_code"])

    response = attempt(
        cod_client,
        seller,
        "POST",
        f"/v1/orders/{order_id}/confirm-pickup",
        idempotency_key,
        json={"pickup_code": code},
    )

    assert_refused(response, "DELIVERED")
    assert_the_row_is_untouched(order_id, stored_orders, stored_reservations)


def test_cancel_refuses_a_lapsed_unpersisted_pickup(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    backdate_ready: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """A party walks away, and the derived delivery refuses before the seller's own refusal does.

    On a COD order the seller may cancel while it is still `CREATED`, but after acceptance the
    state table gives `CANCELLED` no edge and the seller's answer is a `403`. Aimed at a lapsed
    pickup, however, the route reads `DELIVERED` first: the state's `409` is the whole answer,
    and the seller-specific refusal is never reached.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id = lapsed_unpersisted_pickup(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key, backdate_ready
    )

    response = attempt(cod_client, seller, "POST", f"/v1/orders/{order_id}/cancel", idempotency_key)

    assert_refused(response, "CANCELLED")
    assert_the_row_is_untouched(order_id, stored_orders, stored_reservations)


def test_record_weight_refuses_a_lapsed_unpersisted_pickup(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    backdate_ready: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """A weight inside the tolerance is refused anyway, because the pickup is no longer open."""
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id = lapsed_unpersisted_pickup(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key, backdate_ready
    )

    response = attempt(
        cod_client,
        seller,
        "PUT",
        f"/v1/orders/{order_id}/weight",
        idempotency_key,
        json={"delivered_weight_kg": RECORDED_WEIGHT_KG},
    )

    assert_refused(response, "delivered_weight_kg")
    assert_the_row_is_untouched(order_id, stored_orders, stored_reservations)
