"""A buyer-arranged delivery still travels the shipment road (FR-704).

A listing may offer `TRANSPORT_PARTNER`: the buyer arranges their own cart, so no delivery fee
attaches and the platform carries no liability for the goods in transit. Creation accepted the
method - the listing offers it, `_terms` admits any offered option, and the order stores it - but
the order could not move. `mark_shipped` asked for seller delivery and `mark_ready` asks for
pickup, so a transport-partner order sat at `CONFIRMED` (COD) or `FUNDED` (escrow) forever, and
none of the four roads into `DELIVERED` was reachable.

The shipment road is the one that fits: FR-704 says a transport-partner delivery is confirmed the
same as a shipped one. `mark_shipped` now accepts the method, and everything downstream - the
buyer's confirmation, the crop's holding window, the `delivered_via` marker - is unchanged, because
those key on the order's status and not on who carries the goods. `mark_ready` still refuses it,
because a transport-partner order has no pickup to be ready for.

Every assertion reads the row from PostgreSQL, never the API response: `delivered_via` is internal,
and the point of these tests is what was stored, not what was echoed back.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

# SPEC A20: this file drives the real app against an ephemeral Postgres through a client fixture,
# so it is an integration-layer test.
pytestmark = pytest.mark.integration

CHEAP_LISTING_KG = 2_000
CHEAP_ASK_SANTIM = 1_000
CHEAP_DELIVERY_FEE_SANTIM = 150_000
COD_BUY_KG = 200


def post_listing(
    client: TestClient,
    headers: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    **overrides: object,
) -> str:
    """Post a listing and hand back its identity."""
    response = client.post("/v1/listings", headers=headers, json=listing_body(**overrides))
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def buy_now(
    client: TestClient,
    headers: dict[str, str],
    listing_id: str,
    order_body: Callable[..., dict[str, object]],
    key: Callable[[], str],
    **overrides: object,
) -> dict[str, object]:
    """Buy now and hand back the created order."""
    response = client.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**headers, "Idempotency-Key": key()},
        json=order_body(**overrides),
    )
    assert response.status_code == 201, response.text
    order: dict[str, object] = response.json()
    return order


def confirm(client: TestClient, headers: dict[str, str], order_id: str) -> dict[str, object]:
    """Confirm a COD order and hand back the read."""
    response = client.post(f"/v1/orders/{order_id}/confirm", headers=headers)
    assert response.status_code == 200, response.text
    order: dict[str, object] = response.json()
    return order


def attempt(
    client: TestClient,
    headers: dict[str, str],
    path: str,
    key: Callable[[], str],
) -> httpx.Response:
    """Call a fulfillment route, answering either way."""
    response = client.post(path, headers={**headers, "Idempotency-Key": key()})
    assert isinstance(response, httpx.Response)
    return response


def shipped(
    client: TestClient, headers: dict[str, str], order_id: str, key: Callable[[], str]
) -> dict[str, object]:
    """Mark an order shipped and hand back the read."""
    response = attempt(client, headers, f"/v1/orders/{order_id}/shipped", key)
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


def validated_order(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    key: Callable[[], str],
    *,
    delivery_options: list[str],
    delivery_method: str,
    delivery_fee_santim: int,
) -> str:
    """Stand up a confirmed COD order of the given method and hand back its identity."""
    listing_id = post_listing(
        client,
        seller,
        listing_body,
        quantity_kg=CHEAP_LISTING_KG,
        unit_price_santim=CHEAP_ASK_SANTIM,
        delivery_options=delivery_options,
        delivery_fee_santim=delivery_fee_santim,
    )
    order = buy_now(
        client,
        buyer,
        listing_id,
        order_body,
        key,
        payment_mode="COD",
        quantity_kg=COD_BUY_KG,
        delivery_method=delivery_method,
    )
    order_id = str(order["id"])
    confirm(client, buyer, order_id)
    confirm(client, seller, order_id)
    return order_id


def transport_partner_order(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    key: Callable[[], str],
) -> str:
    """A confirmed COD order whose method is buyer-arranged transport."""
    return validated_order(
        client,
        seller,
        buyer,
        listing_body,
        order_body,
        key,
        delivery_options=["TRANSPORT_PARTNER"],
        delivery_method="TRANSPORT_PARTNER",
        delivery_fee_santim=0,
    )


def pickup_order(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    key: Callable[[], str],
) -> str:
    """A confirmed COD pickup order."""
    return validated_order(
        client,
        seller,
        buyer,
        listing_body,
        order_body,
        key,
        delivery_options=["PICKUP"],
        delivery_method="PICKUP",
        delivery_fee_santim=0,
    )


def seller_delivery_order(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    key: Callable[[], str],
) -> str:
    """A confirmed COD seller-delivery order."""
    return validated_order(
        client,
        seller,
        buyer,
        listing_body,
        order_body,
        key,
        delivery_options=["SELLER_DELIVERY"],
        delivery_method="SELLER_DELIVERY",
        delivery_fee_santim=CHEAP_DELIVERY_FEE_SANTIM,
    )


def test_a_transport_partner_order_can_be_marked_shipped(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The seller can put a buyer-arranged delivery on the road, and the row records it.

    Before the fix this was the first step that did not exist: the order was created and confirmed
    and then nothing the seller could send would move it. The row, not the response, is what says
    the shipment happened - `SHIPPED` and a `shipped_at`.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id = transport_partner_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key
    )

    marked = shipped(cod_client, seller, order_id, idempotency_key)

    assert marked["status"] == "SHIPPED"
    row = stored_orders()[0]
    assert row["status"] == "SHIPPED"
    assert row["shipped_at"] is not None
    assert row["delivery_method"] == "TRANSPORT_PARTNER"


def test_a_transport_partner_order_can_reach_delivered(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The buyer's confirmation closes a buyer-arranged delivery like any shipment.

    FR-704 says the confirmation is the same as a shipped order's, and that is what the row shows:
    `DELIVERED` with `delivered_via = BUYER_CONFIRMED`, recorded from the row rather than the API
    because the marker is internal.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id = transport_partner_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key
    )
    shipped(cod_client, seller, order_id, idempotency_key)

    delivered = delivery_confirm(cod_client, buyer, order_id, idempotency_key)

    assert delivered["status"] == "DELIVERED"
    row = stored_orders()[0]
    assert row["status"] == "DELIVERED"
    assert row["delivered_via"] == "BUYER_CONFIRMED"


def test_a_transport_partner_order_is_subject_to_the_shipment_window(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    backdate_shipped: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """A buyer-arranged shipment nobody confirms is still released by the crop's window.

    The window keys on the order's status and the crop's holding hours, so a transport-partner
    order inherits it unchanged - the derivation is the same one every shipped order gets. What
    the read still does here is judge; the row and the `SHIPMENT_WINDOW` marker are written by
    the worker's sweep, and that write is proved in the sweep's own suite.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id = transport_partner_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key
    )
    shipped(cod_client, seller, order_id, idempotency_key)
    backdate_shipped(order_id)

    read = cod_client.get(f"/v1/orders/{order_id}", headers=buyer).json()

    assert read["status"] == "DELIVERED"
    row = stored_orders()[0]
    assert row["status"] == "SHIPPED"
    assert row["delivered_via"] is None


def test_the_method_mismatch_still_refuses(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Widening the shipment gate for transport did not open readiness, and did not close delivery.

    Three answers on three orders: a transport-partner order has no readiness to mark, so `ready`
    refuses with the method named; a pickup order has no shipment to mark, so `shipped` refuses the
    same way; and the seller-delivery order the gate was built for still ships, so the fix is an
    addition and not a replacement.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()

    transport_id = transport_partner_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key
    )
    ready_refusal = attempt(cod_client, seller, f"/v1/orders/{transport_id}/ready", idempotency_key)
    assert ready_refusal.status_code == 409, ready_refusal.text
    error = ready_refusal.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["requested"] == "READY_FOR_PICKUP"
    assert error["details"]["delivery_method"] == "TRANSPORT_PARTNER"

    pickup_id = pickup_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    ship_refusal = attempt(cod_client, seller, f"/v1/orders/{pickup_id}/shipped", idempotency_key)
    assert ship_refusal.status_code == 409, ship_refusal.text
    error = ship_refusal.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["requested"] == "SHIPPED"
    assert error["details"]["delivery_method"] == "PICKUP"

    delivery_id = seller_delivery_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key
    )
    accepted = shipped(cod_client, seller, delivery_id, idempotency_key)
    assert accepted["status"] == "SHIPPED"


def test_a_transport_partner_order_still_requires_the_seller(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Marking a transport-partner order shipped is still the seller's act alone.

    Widening the method check did not widen who may send it: the buyer's request is refused before
    the method is ever consulted, with the same reason the other fulfillment routes give.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id = transport_partner_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key
    )

    refusal = attempt(cod_client, buyer, f"/v1/orders/{order_id}/shipped", idempotency_key)

    assert refusal.status_code == 403, refusal.text
    error = refusal.json()["error"]
    assert error["code"] == "FORBIDDEN"
    assert error["details"]["reason"] == "not_the_seller"
