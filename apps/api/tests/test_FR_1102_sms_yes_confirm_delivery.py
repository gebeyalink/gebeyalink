"""FR-1102, ``YES`` on a delivery token: the buyer's word closes a shipment.

FR-702 makes the buyer the one who confirms that a shipped order arrived. Over SMS the same
confirmation is a YES against a ``delivery_confirm`` token, and it must reach the same state the
app's route reaches: the order delivered, its stamp set and its reservation consumed. This test
drives the COD path so the shipment needs no funding, ships it, then confirms it by text.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.providers.sms import RecordingSmsProvider

pytestmark = pytest.mark.integration

SELLER = "+251911000001"
BUYER = "+251911000003"

LISTING_KG = 1_000
LISTING_ASK_SANTIM = 1_000
BUY_KG = 100
DELIVERY_FEE_SANTIM = 5_000


def _shipped_cod_order(
    cod_client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> dict[str, object]:
    """A COD seller-delivery order standing at SHIPPED, waiting for the buyer's word."""
    created = cod_client.post(
        "/v1/listings",
        headers=seller,
        json=listing_body(
            quantity_kg=LISTING_KG,
            unit_price_santim=LISTING_ASK_SANTIM,
            delivery_options=["SELLER_DELIVERY"],
            delivery_fee_santim=DELIVERY_FEE_SANTIM,
        ),
    )
    assert created.status_code == 201, created.text
    listing_id = created.json()["id"]
    placed = cod_client.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**buyer, "Idempotency-Key": idempotency_key()},
        json=order_body(payment_mode="COD", quantity_kg=BUY_KG, delivery_method="SELLER_DELIVERY"),
    )
    assert placed.status_code == 201, placed.text
    order: dict[str, object] = placed.json()
    order_id = str(order["id"])

    for headers in (buyer, seller):
        confirmed = cod_client.post(f"/v1/orders/{order_id}/confirm", headers=headers)
        assert confirmed.status_code == 200, confirmed.text
    sent = cod_client.post(
        f"/v1/orders/{order_id}/shipped",
        headers={**seller, "Idempotency-Key": idempotency_key()},
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "SHIPPED"
    return order


def test_yes_confirms_a_shipped_delivery(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    issue_action_token: Callable[..., str],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    seller = cod_signed_in(SELLER)
    buyer = cod_signed_in_as_buyer(BUYER)
    language = cod_client.patch("/v1/me", headers=buyer, json={"language": "en"})
    assert language.status_code == 200, language.text

    order = _shipped_cod_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    token = issue_action_token(BUYER, f"delivery_confirm:{order['id']}")

    response = sms_inbound(cod_client, f"YES {token}", from_phone=BUYER)
    assert response.status_code == 200
    assert response.content == b""
    assert (
        sms_recorder.messages_to(BUYER)[-1].body
        == f"Your order {order['short_code']} has been delivered"
    )

    row = stored_orders()[0]
    assert row["status"] == "DELIVERED"
    assert row["delivered_at"] is not None
    assert stored_reservations()[0]["state"] == "CONSUMED"
