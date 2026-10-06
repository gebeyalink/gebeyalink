"""Walking away from a COD order before either side has signed for it.

FR-2103's cancellation is the COD mirror of escrow's: an escrow order is alive only while it
waits for money and cancellable only by the buyer; a COD order is alive only while it waits for
signatures and cancellable by either party, because nothing has moved in either direction yet.
Both parties can walk away in good faith — the seller has no one to blame for walking away from
an unsigned delivery, and the buyer has no one to blame for walking away from an unsigned
purchase — and the kilos go back to the listing either way.

Once both parties have signed, neither may walk: it would be one side keeping the other prisoner
to a promise of delivery against a promise of payment that the platform has already hung its
reputation on. The guard's 409 is the same one every wrong-state refusal wears, and it answers
the seller and the buyer alike.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

# SPEC A20: this file drives the real app against an ephemeral Postgres through a client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

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


def attempt_cancel(
    client: TestClient,
    headers: dict[str, str],
    order_id: str,
    idempotency_key: Callable[[], str],
) -> httpx.Response:
    """Cancel an order, answering either way - for the tests whose point is the refusal."""
    response = client.post(
        f"/v1/orders/{order_id}/cancel",
        headers={**headers, "Idempotency-Key": idempotency_key()},
    )
    assert isinstance(response, httpx.Response)
    return response


def cancel(
    client: TestClient,
    headers: dict[str, str],
    order_id: str,
    idempotency_key: Callable[[], str],
) -> dict[str, object]:
    """Cancel an order and hand back the read."""
    response = attempt_cancel(client, headers, order_id, idempotency_key)
    assert response.status_code == 200, response.text
    order: dict[str, object] = response.json()
    return order


def cod_order(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> tuple[str, str]:
    """Stand up a COD order and hand back its identity and the listing."""

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
    return str(order["id"]), listing_id


def test_the_buyer_can_cancel_a_cod_order_before_confirmation(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """The buyer's walk-away is the same departing as escrow's, minus the funding rules.

    Nothing has moved — the buyer has not paid and has not signed — so the order ends `CANCELLED`
    with the buyer's own reason recorded, the kilos `RELEASED` (not `CONVERTED`, not still held),
    and the crop back on the listing for the next buyer to see.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, listing_id = cod_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key
    )

    cancelled = cancel(cod_client, buyer, order_id, idempotency_key)

    assert cancelled["status"] == "CANCELLED"
    assert cancelled["cancel_reason"] == "BUYER_CANCELLED"
    assert cancelled["payment_mode"] == "COD"
    assert stored_reservations()[0]["state"] == "RELEASED"

    after = cod_client.get(f"/v1/listings/{listing_id}", headers=buyer)
    assert after.json()["available_kg"] == CHEAP_LISTING_KG


def test_the_seller_can_cancel_a_cod_order_before_confirmation(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """The seller's walk-away is the COD difference: it is allowed, and it is their own.

    An escrow seller cannot cancel because acceptance means commitment against an established
    payment promise. On COD nothing is established - both parties are still deciding - so the
    seller's word weighs the same as the buyer's. Their reason is `OTHER` because `BUYER_CANCELLED`
    would be an ordinary lie told about their own walk-away.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, listing_id = cod_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key
    )

    cancelled = cancel(cod_client, seller, order_id, idempotency_key)

    assert cancelled["status"] == "CANCELLED"
    assert cancelled["cancel_reason"] == "OTHER"
    assert stored_reservations()[0]["state"] == "RELEASED"

    after = cod_client.get(f"/v1/listings/{listing_id}", headers=seller)
    assert after.json()["available_kg"] == CHEAP_LISTING_KG


def test_nobody_cancels_a_confirmed_cod_order(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """After both signatures, the seller's side of the bargain is not theirs to renounce.

    The buyer walks away first and is told `409`: the confirmation has made the crop `CONVERTED`,
    so nothing is released and the order's reason stays empty — the refusal answers without
    rewriting a single column.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = cod_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    cod_client.post(f"/v1/orders/{order_id}/confirm", headers=buyer)
    cod_client.post(f"/v1/orders/{order_id}/confirm", headers=seller)

    response = attempt_cancel(cod_client, buyer, order_id, idempotency_key)

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["order_id"] == order_id
    assert error["details"]["status"] == "CONFIRMED"
    assert error["details"]["requested"] == "CANCELLED"
    assert stored_reservations()[0]["state"] == "CONVERTED"


def test_the_seller_gets_the_same_409_after_confirmation(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The guard's 409 is not picky about which party is on the wrong side of it.

    A confirmed order is doubly signed, and whether the would-be runaway is the buyer or the
    seller the answer is the same: the trade has stopped being a negotiation. FR-607's escrow
    403 was about the seller not being allowed to break an *unbroken* commitment; here the
    commitment is already made and neither side is exempt.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = cod_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    cod_client.post(f"/v1/orders/{order_id}/confirm", headers=buyer)
    cod_client.post(f"/v1/orders/{order_id}/confirm", headers=seller)

    response = attempt_cancel(cod_client, seller, order_id, idempotency_key)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ILLEGAL_STATE_TRANSITION"


def test_a_stranger_cannot_cancel_a_cod_order(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    cod_sign_in: Callable[..., httpx.Response],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """A third account is not a party to the order and cannot end it either.

    Cancellation is a party's veto, and exactly like confirmation it falls to the same
    not-a-party 403 that every two-sided read already carries.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = cod_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)

    verified = cod_sign_in("+251911000002")
    stranger = {"Authorization": f"Bearer {verified.json()['access_token']}"}
    response = attempt_cancel(cod_client, stranger, order_id, idempotency_key)

    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["code"] == "FORBIDDEN"
    assert error["details"]["reason"] == "not_a_party_to_this_order"


def test_an_escrow_seller_still_cannot_cancel_after_acceptance(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The whole COD slice leaves FR-607's answer exactly as it was.

    An escrow order is still a commitment the seller cannot renounce — COD's seller walk-away is
    the exception that makes the rule visible: the reason it is not 403'd is the absence of a
    buyer's commitment, not a change of heart about sellers. Where the buyer has funded nothing
    and committed nothing, the seller still gets `seller_cannot_cancel_after_acceptance`.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in_as_buyer()
    escrow = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=COD_BUY_KG)

    response = attempt_cancel(client, seller, str(escrow["id"]), idempotency_key)

    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["code"] == "FORBIDDEN"
    assert error["details"]["reason"] == "seller_cannot_cancel_after_acceptance"
