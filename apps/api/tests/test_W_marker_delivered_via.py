"""Which mechanism delivered the order, recorded where the delivery happened.

Every road into `DELIVERED` passes through one settlement chokepoint, and the row used to say
nothing about which road it took: the status, the timestamp and - for the two pickup roads - a
spent pickup code are what a confirmation and a closed window leave behind equally. The mechanism
is guessable from `delivered_at` against a window boundary, but the boundaries read a crop's
holding hours (editable after the sale) and a code constant, so the guess is not durable: the same
delivery can be attributed differently on two days. `orders.delivered_via` records the mechanism
at the moment it happened instead.

The field is internal. Every assertion here reads it from the row, never from the API, because it
is not in `OrderResponse` - and one test below is exactly that claim.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

# SPEC A20: this file drives the real app against an ephemeral Postgres through a client
# fixture, so it is an integration-layer test.
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
    idempotency_key: Callable[[], str],
    **overrides: object,
) -> dict[str, object]:
    """Buy the listing outright and hand back the order read."""
    response = client.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**headers, "Idempotency-Key": idempotency_key()},
        json=order_body(**overrides),
    )
    assert response.status_code == 201, response.text
    order: dict[str, object] = response.json()
    return order


def confirm(client: TestClient, headers: dict[str, str], order_id: str) -> dict[str, object]:
    """Confirm the COD agreement and hand back the read."""
    response = client.post(f"/v1/orders/{order_id}/confirm", headers=headers)
    assert response.status_code == 200, response.text
    order: dict[str, object] = response.json()
    return order


def attempt(
    client: TestClient,
    headers: dict[str, str],
    path: str,
    idempotency_key: Callable[[], str],
    *,
    json: dict[str, Any] | None = None,
) -> httpx.Response:
    """Call a fulfillment route, answering either way."""
    request: dict[str, object] = {}
    if json is not None:
        request["json"] = json
    response = client.post(
        path, headers={**headers, "Idempotency-Key": idempotency_key()}, **request
    )
    assert isinstance(response, httpx.Response)
    return response


def ready(
    client: TestClient, headers: dict[str, str], order_id: str, key: Callable[[], str]
) -> dict[str, object]:
    """Mark a pickup order ready and hand back the read."""
    response = attempt(client, headers, f"/v1/orders/{order_id}/ready", key)
    assert response.status_code == 200, response.text
    order: dict[str, object] = response.json()
    return order


def shipped(
    client: TestClient, headers: dict[str, str], order_id: str, key: Callable[[], str]
) -> dict[str, object]:
    """Mark a seller-delivery order shipped and hand back the read."""
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
) -> str:
    """Stand up a validated COD pickup order and hand back its identity."""
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
    )
    return str(order["id"])


def delivery_order(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> str:
    """Stand up a validated COD seller-delivery order and hand back its identity."""
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
    return str(order["id"])


def pickup_code_of(client: TestClient, headers: dict[str, str], order_id: str) -> str:
    """The buyer's copy of the code, read the way the app itself would."""
    read = client.get(f"/v1/orders/{order_id}", headers=headers).json()
    code = read["pickup_code"]
    assert code is not None, read
    return str(code)


def _sync_url(postgres_url: str) -> str:
    return postgres_url.replace("postgresql+psycopg://", "postgresql://")


def test_the_buyer_s_confirmation_is_recorded_as_the_mechanism(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The buyer's own word closes the delivery, and the row says so.

    A seller-delivery order reaches `DELIVERED` when the buyer confirms it arrived. The row
    records `BUYER_CONFIRMED`, because that is the road that was taken and not merely one of the
    roads that could have been.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id = delivery_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    shipped(cod_client, seller, order_id, idempotency_key)

    delivered = delivery_confirm(cod_client, buyer, order_id, idempotency_key)

    assert delivered["status"] == "DELIVERED"
    assert stored_orders()[0]["delivered_via"] == "BUYER_CONFIRMED"


def test_the_pickup_code_is_recorded_as_the_mechanism(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The seller breaks the code at the handover, and the row says so.

    A pickup closes on the code, not on a window or a buyer's word downstream. The row records
    `PICKUP_CODE`, and it is a different value from the pickup-window road even though both spend
    the same code and leave the same `DELIVERED` status.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id = pickup_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    code = pickup_code_of(cod_client, buyer, order_id)
    ready(cod_client, seller, order_id, idempotency_key)

    delivered = pickup_confirm(cod_client, seller, order_id, idempotency_key, code)

    assert delivered["status"] == "DELIVERED"
    assert stored_orders()[0]["delivered_via"] == "PICKUP_CODE"


def test_the_pickup_window_is_recorded_as_the_mechanism(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    backdate_ready: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """Nobody came and the window closed; the row records that, not a confirmation.

    The automatic roads are the ones that most need recording, because no party acted and there is
    no confirmation to point at. `PICKUP_WINDOW` is the fact, written by the read that did the
    settling.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id = pickup_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    ready(cod_client, seller, order_id, idempotency_key)
    backdate_ready(order_id)

    read = cod_client.get(f"/v1/orders/{order_id}", headers=buyer).json()

    assert read["status"] == "DELIVERED"
    assert stored_orders()[0]["delivered_via"] == "PICKUP_WINDOW"


def test_a_lapsed_shipment_reads_as_delivered_before_the_worker_writes_it(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    backdate_shipped: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The crop's holding window passed and the read says so; the row waits for the worker.

    A shipment whose buyer never confirms is released by the window, measured from `shipped_at`
    against the crop's holding hours, and the read reports that delivery immediately. The marker
    for it - `SHIPMENT_WINDOW` - is the worker's write, and this pins the other half of the
    arrangement: until the sweep runs, the row still says shipped and carries no mechanism.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id = delivery_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    shipped(cod_client, seller, order_id, idempotency_key)
    backdate_shipped(order_id)

    read = cod_client.get(f"/v1/orders/{order_id}", headers=buyer).json()

    assert read["status"] == "DELIVERED"
    row = stored_orders()[0]
    assert row["status"] == "SHIPPED"
    assert row["delivered_via"] is None


def test_an_order_that_has_not_been_delivered_has_no_mechanism(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """`NULL` means "not delivered", never "we do not know".

    An order that has moved but is still waiting at a pickup or on the road has no delivery
    mechanism to record, so the column stays null. It is filled exactly once, at the settlement.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id = pickup_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)

    row = stored_orders()[0]

    assert row["status"] == "CONFIRMED"
    assert row["delivered_via"] is None


def test_the_column_is_nullable_without_a_default_so_nothing_is_backfilled(
    postgres_url: str,
) -> None:
    """The migration adds a nullable enum column and no default, and computes nothing.

    A server default would have silently written a value onto every order that existed before the
    column, and whichever value it wrote would be a guess about a delivery nobody recorded. The
    column is nullable with no default, so pre-existing rows stay `NULL` - the honest answer for a
    mechanism that was never written down - and the type is the enum named for the column rather
    than a bare string.
    """
    with (
        psycopg.connect(_sync_url(postgres_url)) as connection,
        connection.cursor(row_factory=psycopg.rows.dict_row) as cursor,
    ):
        cursor.execute(
            "SELECT is_nullable, column_default, udt_name FROM information_schema.columns"
            " WHERE table_name = 'orders' AND column_name = 'delivered_via'"
        )
        fetched: dict[str, object] = dict(cursor.fetchone() or {})

    assert fetched["is_nullable"] == "YES"
    assert fetched["column_default"] is None
    assert fetched["udt_name"] == "delivered_via"


def test_a_delivered_row_whose_mechanism_is_unset_is_not_recomputed_on_read(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    postgres_url: str,
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """A pre-existing delivered row stands in, and a later read does not fill it in.

    This makes the pre-existing-row case runnable: a delivered order has its mechanism cleared
    behind the API the way every row that predates the column stands. A read afterwards must not
    derive a mechanism from the timestamps and write it back - a backfilled guess would rewrite
    when the delivery happened, which is the whole reason the column exists.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id = delivery_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    shipped(cod_client, seller, order_id, idempotency_key)
    delivery_confirm(cod_client, buyer, order_id, idempotency_key)
    order_row = stored_orders()[0]
    assert order_row["delivered_via"] == "BUYER_CONFIRMED"

    with psycopg.connect(_sync_url(postgres_url), autocommit=True) as connection:
        connection.execute(
            "UPDATE orders SET delivered_via = NULL WHERE id = %s", (order_row["id"],)
        )

    cod_client.get(f"/v1/orders/{order_id}", headers=buyer)

    assert stored_orders()[0]["delivered_via"] is None


def test_the_mechanism_is_not_in_the_order_response(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The mechanism is internal: the row holds it and the response does not carry it.

    `delivered_via` is a record for the platform, not a fact for a client, so it is absent from
    `OrderResponse` and from the response body entirely. The row proving the write is what makes
    the absence meaningful rather than vacuous.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id = delivery_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    shipped(cod_client, seller, order_id, idempotency_key)
    delivery_confirm(cod_client, buyer, order_id, idempotency_key)
    assert stored_orders()[0]["delivered_via"] == "BUYER_CONFIRMED"

    response = cod_client.get(f"/v1/orders/{order_id}", headers=buyer)

    assert response.status_code == 200, response.text
    assert "delivered_via" not in response.json()
    assert "delivered_via" not in response.text
