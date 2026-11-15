"""FR-2101: every money-gate reader asks the flag table, and the order records the answer.

The old switches were process values, so a test could arrange them by building a different app.
They are rows now, and this file drives the same application against each arrangement. The
checkout path, the two feature-phone adapters and the order's frozen payment mode are the
readers this slice changes; each test asserts the database row as well as the response, because
the row is the evidence the request did not merely echo what it was sent.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.integration]

CHEAP_LISTING_KG = 2_000
CHEAP_ASK_SANTIM = 1_000
BUY_KG = 200

SELLER = "+251911000001"
BUYER = "+251911000003"
SESSION_ID = "ses-flag-wiring-1"


def _post_listing(
    client: TestClient,
    seller: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    **overrides: object,
) -> dict[str, object]:
    response = client.post("/v1/listings", headers=seller, json=listing_body(**overrides))
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


def _as_seller(client: TestClient, signed_in: Callable[..., dict[str, str]]) -> dict[str, str]:
    """A seller signed in and given the pilot region, so a listing can be posted."""
    headers = signed_in(SELLER)
    updated = client.patch(
        "/v1/me",
        headers=headers,
        json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
    )
    assert updated.status_code == 200, updated.text
    return headers


def _as_buyer(
    client: TestClient, signed_in_as_buyer: Callable[..., dict[str, str]]
) -> dict[str, str]:
    """A buyer signed in and given the pilot region, so an order could name an address."""
    headers = signed_in_as_buyer(BUYER)
    updated = client.patch(
        "/v1/me",
        headers=headers,
        json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
    )
    assert updated.status_code == 200, updated.text
    return headers


def _walk_to_the_offer(
    client: TestClient, ussd_session: Callable[..., httpx.Response]
) -> httpx.Response:
    """Press through the browse path to the confirm step and take it."""
    for keypress in ["", "2", "8", "1", "1", "1", "500", "30"]:
        ussd_session(client, keypress, session_id=SESSION_ID, phone_number=BUYER)
    sent: httpx.Response = ussd_session(client, "1", session_id=SESSION_ID, phone_number=BUYER)
    return sent


def test_buy_now_reads_the_cod_flag_and_snapshots_the_mode(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """With COD on, choosing it creates a COD order and the row keeps that answer."""
    seller = _as_seller(cod_client, cod_signed_in)
    listing = _post_listing(
        cod_client,
        seller,
        listing_body,
        quantity_kg=CHEAP_LISTING_KG,
        unit_price_santim=CHEAP_ASK_SANTIM,
    )
    buyer = _as_buyer(cod_client, cod_signed_in_as_buyer)

    response = cod_client.post(
        f"/v1/listings/{listing['id']}/buy-now",
        headers={**buyer, "Idempotency-Key": idempotency_key()},
        json=order_body(payment_mode="COD", quantity_kg=BUY_KG),
    )

    assert response.status_code == 201, response.text
    order = response.json()
    assert order["payment_mode"] == "COD"
    assert order["status"] == "CREATED"
    assert stored_orders()[0]["payment_mode"] == "COD"


def test_buy_now_reads_the_escrow_flag_and_snapshots_the_default(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """With escrow on and no mode in the body, the default resolves against the flag."""
    seller = _as_seller(client, signed_in)
    listing = _post_listing(
        client,
        seller,
        listing_body,
        quantity_kg=CHEAP_LISTING_KG,
        unit_price_santim=CHEAP_ASK_SANTIM,
    )
    buyer = _as_buyer(client, signed_in_as_buyer)

    response = client.post(
        f"/v1/listings/{listing['id']}/buy-now",
        headers={**buyer, "Idempotency-Key": idempotency_key()},
        json=order_body(quantity_kg=BUY_KG),
    )

    assert response.status_code == 201, response.text
    order = response.json()
    assert order["payment_mode"] == "ESCROW"
    assert order["status"] == "AWAITING_FUNDING"
    assert stored_orders()[0]["payment_mode"] == "ESCROW"


def test_buy_now_refuses_escrow_by_name_when_the_flag_is_off(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The flag is the mode's existence, so asking for escrow under COD is a 422 by name."""
    seller = _as_seller(cod_client, cod_signed_in)
    listing = _post_listing(
        cod_client,
        seller,
        listing_body,
        quantity_kg=CHEAP_LISTING_KG,
        unit_price_santim=CHEAP_ASK_SANTIM,
    )
    buyer = _as_buyer(cod_client, cod_signed_in_as_buyer)

    refused = cod_client.post(
        f"/v1/listings/{listing['id']}/buy-now",
        headers={**buyer, "Idempotency-Key": idempotency_key()},
        json=order_body(payment_mode="ESCROW", quantity_kg=BUY_KG),
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["details"] == {"feature": "escrow"}
    assert stored_orders() == []


def test_buy_now_refuses_cod_by_name_when_the_flag_is_off(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The other flag is the same rule read from the other side."""
    seller = _as_seller(client, signed_in)
    listing = _post_listing(
        client,
        seller,
        listing_body,
        quantity_kg=CHEAP_LISTING_KG,
        unit_price_santim=CHEAP_ASK_SANTIM,
    )
    buyer = _as_buyer(client, signed_in_as_buyer)

    refused = client.post(
        f"/v1/listings/{listing['id']}/buy-now",
        headers={**buyer, "Idempotency-Key": idempotency_key()},
        json=order_body(payment_mode="COD", quantity_kg=BUY_KG),
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["details"] == {"feature": "cod"}
    assert stored_orders() == []


def test_an_sms_offer_reads_the_cod_flag(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    sms_inbound: Callable[..., httpx.Response],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """The feature-phone grammar carries no mode, so the adapter asks the flag table."""
    seller = _as_seller(cod_client, cod_signed_in)
    listing = _post_listing(cod_client, seller, listing_body)
    _as_buyer(cod_client, cod_signed_in_as_buyer)

    response = sms_inbound(cod_client, f"OFFER {listing['short_code']} 500 33", from_phone=BUYER)

    assert response.status_code == 200
    offers = stored_offers()
    assert len(offers) == 1
    assert offers[0]["payment_mode"] == "COD"


def test_a_ussd_offer_reads_the_cod_flag(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    ussd_session: Callable[..., httpx.Response],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """The USSD tree has no mode screen either, so it reads the same flag."""
    seller = _as_seller(cod_client, cod_signed_in)
    _post_listing(cod_client, seller, listing_body)
    _as_buyer(cod_client, cod_signed_in_as_buyer)

    sent = _walk_to_the_offer(cod_client, ussd_session)

    assert sent.text.startswith("END ")
    offers = stored_offers()
    assert len(offers) == 1
    assert offers[0]["payment_mode"] == "COD"
