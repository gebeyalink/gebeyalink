"""Checkout that agrees to pay on delivery instead of funding the order.

FR-2101's COD path is a choice in the buy-now body, and the test below treats it the way any
choice is treated: the default works, the explicit choice works, and the paths the platform has
deliberately not opened yet refuse by name. The two refusals are different words because they
are different rules — a mode nobody switched on is a request for something that does not exist,
while a mode that exists but will not carry this much is an order over a limit — and they carry
different numbers, so a client can tell its user which is which without guessing.

The money side matters too. A COD order never moves money, and the schema is the evidence:
there is no escrow account, payment or ledger table for this platform to write to yet, and an
assertion that those tables don't exist is the strongest form of "no escrow row ever".
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

# SPEC A20: this file drives the real app against an ephemeral Postgres through a client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

#: Ten thousand birr, in santim: the goods value a COD pilot will extend before it must decide
#: again. The rejection below proves the boundary by standing one santim outside it.
CAP_SANTIM = 1_000_000

#: A listing worth a safe distance below the ceiling: two hundred kilos at ten birr a kilo is a
#: two-thousand-birr order, comfortably inside a pilot that extends ten thousand.
CHEAP_LISTING_KG = 2_000
CHEAP_ASK_SANTIM = 1_000
COD_BUY_KG = 200


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


def test_a_cod_order_is_created_created_and_holds_its_kilos_without_funding(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """Choosing COD makes a `CREATED` order that never entered the funding queue.

    An escrow order moves to `AWAITING_FUNDING` in the same transaction that reserves its kilos;
    nothing here has any money to wait for, so a COD order that claimed to be waiting would be
    lying about the trade. It stays `CREATED`, and its kilos are `HELD` exactly as escrow's are —
    the listing's availability has to fall either way, because the crop is spoken for regardless
    of how it will be paid for.
    """
    seller = cod_signed_in()
    listing_id = post_listing(
        cod_client,
        seller,
        listing_body,
        quantity_kg=CHEAP_LISTING_KG,
        unit_price_santim=CHEAP_ASK_SANTIM,
    )
    buyer = cod_signed_in_as_buyer()

    order = buy_now(
        cod_client,
        buyer,
        listing_id,
        order_body,
        idempotency_key,
        payment_mode="COD",
        quantity_kg=COD_BUY_KG,
    )

    assert order["status"] == "CREATED"
    assert order["payment_mode"] == "COD"
    assert order["funded_at"] is None
    assert order["confirmed_at"] is None
    assert order["seller_delayed"] is False

    stored = stored_orders()[0]
    assert stored["payment_mode"] == "COD"
    assert stored["status"] == "CREATED"
    assert stored["confirmed_at"] is None
    assert stored["buyer_confirmed_at"] is None
    assert stored["seller_confirmed_at"] is None

    held = stored_reservations()
    assert len(held) == 1
    assert held[0]["state"] == "HELD"
    assert held[0]["quantity_kg"] == COD_BUY_KG

    after = cod_client.get(f"/v1/listings/{listing_id}", headers=buyer)
    assert after.json()["available_kg"] == CHEAP_LISTING_KG - COD_BUY_KG


def test_checkout_defaults_to_escrow(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """Leaving the body silent is an escrow order.

    A mode that is inherited rather than chosen keeps every client written before the field
    existed working as it always did: nobody has to send a mode to keep getting the platform's
    default, and the default is the mode this deployment funds.
    """
    seller = signed_in()
    listing_id = post_listing(
        client,
        seller,
        listing_body,
        quantity_kg=CHEAP_LISTING_KG,
        unit_price_santim=CHEAP_ASK_SANTIM,
    )
    buyer = signed_in_as_buyer()

    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=COD_BUY_KG)

    assert order["payment_mode"] == "ESCROW"
    assert order["status"] == "AWAITING_FUNDING"
    assert stored_orders()[0]["payment_mode"] == "ESCROW"


def test_escrow_is_refused_when_escrow_is_off(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """The other half of the switch: a mode nobody switched on is refused by name.

    This is the COD configuration, which funds COD and not escrow, so asking for escrow is a
    request for something that does not exist here and is answered the same way COD is answered
    where COD is off — 422, naming the mode, writing neither an order nor a reservation.
    """
    seller = cod_signed_in()
    listing_id = post_listing(
        cod_client,
        seller,
        listing_body,
        quantity_kg=CHEAP_LISTING_KG,
        unit_price_santim=CHEAP_ASK_SANTIM,
    )
    buyer = cod_signed_in_as_buyer()

    response = attempt_buy_now(
        cod_client,
        buyer,
        listing_id,
        order_body,
        idempotency_key,
        payment_mode="ESCROW",
        quantity_kg=COD_BUY_KG,
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == {"feature": "escrow"}
    assert stored_orders() == []
    assert stored_reservations() == []


def test_cod_is_refused_by_name_while_the_flag_is_off(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """A mode nobody switched on is a 422 naming the mode, and it writes nothing.

    This is the default application, which is the configuration that owns the refusal: the pilot
    flag is off for everybody until a deployment decides otherwise. The feature is named because
    that is what the client has to fix - it asked for something that does not exist - and the
    refusal writes no order and no reservation, because a failed request that had committed half
    its rows would be a bug twice over.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in_as_buyer()

    response = attempt_buy_now(
        client,
        buyer,
        listing_id,
        order_body,
        idempotency_key,
        payment_mode="COD",
        quantity_kg=COD_BUY_KG,
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == {"feature": "cod"}
    assert stored_orders() == []
    assert stored_reservations() == []


def test_a_cod_order_above_ten_thousand_birr_is_refused_with_the_limit(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """10,000.01 birr stands one santim outside what a pilot will extend.

    The ceiling is on the goods alone, so the refusal names the subtotal and the limit in the
    same unit, and both are stated so a client can show "orders above 10,000 birr are not
    accepted right now" without having to know the platform's internal number.
    """
    seller = cod_signed_in()
    listing_id = post_listing(
        cod_client,
        seller,
        listing_body,
        unit_price_santim=1_000_001,
        min_order_kg=1,
    )
    buyer = cod_signed_in_as_buyer()

    response = attempt_buy_now(
        cod_client,
        buyer,
        listing_id,
        order_body,
        idempotency_key,
        payment_mode="COD",
        quantity_kg=1,
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "LIMIT_EXCEEDED"
    assert error["details"]["limit_santim"] == CAP_SANTIM
    assert error["details"]["subtotal_santim"] == 1_000_001
    assert stored_orders() == []


def test_a_cod_order_of_exactly_ten_thousand_birr_is_accepted(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The boundary belongs to the pilot: exactly 10,000 birr fits, one santim more does not."""
    seller = cod_signed_in()
    listing_id = post_listing(
        cod_client,
        seller,
        listing_body,
        unit_price_santim=CAP_SANTIM,
        min_order_kg=1,
    )
    buyer = cod_signed_in_as_buyer()

    order = buy_now(
        cod_client,
        buyer,
        listing_id,
        order_body,
        idempotency_key,
        payment_mode="COD",
        quantity_kg=1,
    )

    assert order["status"] == "CREATED"
    assert order["payment_mode"] == "COD"
    assert order["unit_price_santim"] == CAP_SANTIM


def test_no_money_tables_exist_for_a_cod_order_to_write_to(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    postgres_url: str,
) -> None:
    """A COD order ends its whole course never having touched money, and the schema is the proof.

    The specification lists `payments`, `escrow_accounts` and the ledger tables, and none of them
    exist in this schema yet. Asserting absence is the strongest form of "no payment intent, no
    escrow row, no ledger — ever": there is nowhere for a COD order to write such a row even if
    the service tried, and "no money moves" is a structural fact rather than a behaviour that
    could quietly regress.
    """
    seller = cod_signed_in()
    listing_id = post_listing(
        cod_client,
        seller,
        listing_body,
        quantity_kg=CHEAP_LISTING_KG,
        unit_price_santim=CHEAP_ASK_SANTIM,
    )
    buyer = cod_signed_in_as_buyer()
    order = buy_now(
        cod_client,
        buyer,
        listing_id,
        order_body,
        idempotency_key,
        payment_mode="COD",
        quantity_kg=COD_BUY_KG,
    )

    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url) as connection:
        rows = connection.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name IN"
            " ('payments', 'escrow_accounts', 'ledger_accounts', 'ledger_entries')"
        ).fetchall()

    assert rows == []
    assert order["funded_at"] is None
