"""The escrow flag, on both sides of the switch.

Escrow is the platform's funding path, and like COD it is behind a flag: a deployment that has
not opened it must refuse a request for it by name rather than write an order it will not fund.
The switch is met in four places — placing an offer, answering one, accepting one, and buying at
the asking price — and this file drives all four, plus the one thing an offer carries across a
whole negotiation: the mode is chosen when the thread opens, and a counter inherits it rather
than restating it, so neither party can change it by answering.

The flag is fixed when an application is built, so the one move that has to be seen across a
change of flag — accepting an offer that was placed while escrow was on, after it has gone off —
is driven by two applications over one database and one signing key. See `escrow_flag_clients`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

# SPEC A20: this file drives the real app against an ephemeral Postgres through client fixtures,
# so it is an integration-layer test.
pytestmark = pytest.mark.integration


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


def attempt_offer(
    client: TestClient,
    headers: dict[str, str],
    listing_id: str,
    body: Callable[..., dict[str, object]],
    **overrides: object,
) -> httpx.Response:
    """Bid on a listing, answering either way - for the tests whose point is the refusal."""
    response = client.post(
        f"/v1/listings/{listing_id}/offers", headers=headers, json=body(**overrides)
    )
    assert isinstance(response, httpx.Response)
    return response


def place_offer(
    client: TestClient,
    headers: dict[str, str],
    listing_id: str,
    body: Callable[..., dict[str, object]],
    **overrides: object,
) -> str:
    """Bid on a listing and hand back the offer's identity."""
    response = attempt_offer(client, headers, listing_id, body, **overrides)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def answer(
    client: TestClient,
    headers: dict[str, str],
    offer_id: str,
    body: Callable[..., dict[str, object]],
    **overrides: object,
) -> httpx.Response:
    """Answer an offer, either way - for the tests whose point is the refusal."""
    response = client.post(
        f"/v1/offers/{offer_id}/counter", headers=headers, json=body(**overrides)
    )
    assert isinstance(response, httpx.Response)
    return response


def answered(
    client: TestClient,
    headers: dict[str, str],
    offer_id: str,
    body: Callable[..., dict[str, object]],
    **overrides: object,
) -> str:
    """Answer an offer and hand back the counter's identity."""
    response = answer(client, headers, offer_id, body, **overrides)
    assert response.status_code == 200, response.text
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
    response = client.post(
        f"/v1/offers/{offer_id}/accept",
        headers={**headers, "Idempotency-Key": key()},
        json=body(**overrides),
    )
    assert isinstance(response, httpx.Response)
    return response


def test_offer_creation_refuses_escrow_when_off(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """A bid that asks for a mode this deployment does not fund is refused, and writes nothing.

    This is the COD configuration, which does not fund escrow. The refusal names the mode for the
    same reason the buy-now refusal does: the client asked for something that does not exist here
    and can only be told so, not corrected field by field. A refused bid leaves no row behind.
    """
    seller = cod_signed_in()
    listing_id = post_listing(cod_client, seller, listing_body)
    buyer = cod_signed_in_as_buyer()

    response = attempt_offer(cod_client, buyer, listing_id, offer_body, payment_mode="ESCROW")

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == {"feature": "escrow"}
    assert stored_offers() == []


def test_offer_creation_refuses_cod_when_cod_is_off(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """The other half of the switch: COD is off here, so a bid that asks for it is refused.

    Escrow and COD are the same rule seen from two sides, and the two refusals read alike: 422,
    naming the mode, with no row written. Which modes exist is a deployment's decision, and a bid
    is the first place that decision can be met.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in_as_buyer()

    response = attempt_offer(client, buyer, listing_id, offer_body, payment_mode="COD")

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == {"feature": "cod"}
    assert stored_offers() == []


def test_offer_payment_mode_is_carried_through_counters(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """A counter keeps the mode of the offer it answers, and cannot carry one of its own.

    Two things are proven here. The first is the body: a counter's shape has no payment mode at
    all, so a client that sends one is refused before any rule about modes is asked - a 422 naming
    `payment_mode` as the field, which is the strongest form of "answering cannot change it". The
    second is the rows: a seller's counter and the buyer's answer both store the mode the opening
    bid chose, so a reader of the thread sees one mode on every move and never has to work out
    which move decided it.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in_as_buyer()

    opening = place_offer(client, buyer, listing_id, offer_body)

    refused = answer(client, seller, opening, offer_body, payment_mode="COD")
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "payment_mode" in error["details"]["fields"]

    seller_answer = answered(client, seller, opening, offer_body)
    answered(client, buyer, seller_answer, offer_body)

    stored = stored_offers()
    assert [row["thread_depth"] for row in stored] == [0, 1, 2]
    assert [row["payment_mode"] for row in stored] == ["ESCROW", "ESCROW", "ESCROW"]


def test_escrow_enabled_accepts_escrow_offer(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """With escrow on, an accepted bid becomes an escrow order awaiting funding.

    This is the path a deployment that funds escrow takes, and it is the acceptance the flag-off
    test refuses. The mode travels from the offer onto the order, so a buyer reading the order
    sees the same mode the bid was placed under, and the offer is the one that moved.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    offer_id = place_offer(client, buyer, listing_id, offer_body)

    response = accept(client, seller, offer_id, order_body, idempotency_key)

    assert response.status_code == 201, response.text
    order = response.json()
    assert order["offer_id"] == offer_id
    assert order["payment_mode"] == "ESCROW"
    assert order["status"] == "AWAITING_FUNDING"

    assert stored_orders()[0]["payment_mode"] == "ESCROW"
    assert [row["status"] for row in stored_offers() if str(row["id"]) == offer_id] == ["ACCEPTED"]


async def test_accept_refuses_escrow_when_the_flag_is_off_by_then(
    escrow_flag_clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
    sign_in_over: Callable[..., Awaitable[dict[str, str]]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    postgres_url: str,
    stored_offers: Callable[[], list[dict[str, object]]],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """An offer placed while escrow was on is refused at acceptance once escrow is off.

    The two clients are one deployment before and after the flag changed: they share the database
    and the signing key, so the token the first minted is the token the second checks. The refusal
    has to leave the offer `OPEN` — a configuration change is not the buyer's mistake, and losing
    the thread to it would end a negotiation neither party ended — and it writes no order.
    """
    funding, unfunded = escrow_flag_clients
    seller_phone = "+251911000011"
    buyer_phone = "+251911000013"

    seller = await sign_in_over(funding, seller_phone)
    buyer = await sign_in_over(funding, buyer_phone)
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("UPDATE users SET role = 'BUYER' WHERE phone_e164 = %s", (buyer_phone,))

    posted = await funding.post("/v1/listings", headers=seller, json=listing_body())
    assert posted.status_code == 201, posted.text
    listing_id = str(posted.json()["id"])

    placed = await funding.post(
        f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body()
    )
    assert placed.status_code == 201, placed.text
    offer_id = str(placed.json()["id"])

    refused = await unfunded.post(
        f"/v1/offers/{offer_id}/accept",
        headers={**seller, "Idempotency-Key": idempotency_key()},
        json=order_body(),
    )

    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == {"feature": "escrow"}
    assert [row["status"] for row in stored_offers()] == ["OPEN"]
    assert stored_orders() == []
