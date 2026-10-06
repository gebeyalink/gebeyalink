"""Two parties signing the same terms instead of money moving.

FR-2102's confirmation is the COD stand-in for funding: an escrow order advances to `FUNDED`
once money arrives, and a COD order advances to `CONFIRMED` once both parties have said — in
either order — that the terms are the ones they signed up for. The route is idempotent by
construction, which is why it is the one mutation that does not ask for `Idempotency-Key`: the
party distribution *is* the guard, a second tap by the same person is a no-op rather than a
second confirmation, and the tests below draw that boundary on both sides of it.

Everything the FLI money would have done is deferred to the delivery: the crop stays reserved
(`HELD` becomes `CONVERTED`, not `RELEASED`) and the order's `confirmed_at` stamps the moment
the second party signs. And an escrow order has no confirmation at all — money must say yes, not
people's signatures, and the guard says so with the same 409 it gives to confirmed cancellations.
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


def attempt_confirm(client: TestClient, headers: dict[str, str], order_id: str) -> httpx.Response:
    """Confirm an order, answering either way - for the tests whose point is the refusal."""
    response = client.post(f"/v1/orders/{order_id}/confirm", headers=headers)
    assert isinstance(response, httpx.Response)
    return response


def confirm(client: TestClient, headers: dict[str, str], order_id: str) -> dict[str, object]:
    """Confirm an order and hand back the read."""
    response = attempt_confirm(client, headers, order_id)
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


def test_both_party_confirmations_move_a_cod_order_to_confirmed(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """The buyer's signature reserves the agreement; the seller's completes it.

    Neither party alone owns the trade — the seller will deliver against the buyer's word, and
    the buyer will pay against the seller's — so one confirmation is a half-signed agreement and
    the order stays `CREATED` (one party cannot hold the other to a word nobody seconded). The
    second confirmation, from either party, is what moves it to `CONFIRMED`, and the kilos stop
    being merely reserved and become committed: `HELD` becomes `CONVERTED`.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = cod_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)

    first = confirm(cod_client, buyer, order_id)
    assert first["status"] == "CREATED"
    assert first["confirmed_at"] is None

    second = confirm(cod_client, seller, order_id)
    assert second["status"] == "CONFIRMED"
    assert second["confirmed_at"] is not None
    assert second["seller_delayed"] is False
    assert second["cancel_reason"] is None

    assert stored_reservations()[0]["state"] == "CONVERTED"


def test_confirmation_is_idempotent_per_party(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """A second tap by the same party is a no-op, before and after the order confirms.

    The party distribution IS the idempotency key: whether the buyer taps twice, or taps again
    after the whole trade has confirmed, the answer is the same 200 with no second transition and
    no re-stamped `confirmed_at` — the order cannot be told something it has already decided.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = cod_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)

    first = confirm(cod_client, buyer, order_id)
    assert first["status"] == "CREATED"

    retap = confirm(cod_client, buyer, order_id)
    assert retap["status"] == "CREATED"
    assert retap["confirmed_at"] is None

    seller_confirm = confirm(cod_client, seller, order_id)

    replay = confirm(cod_client, buyer, order_id)
    assert replay["status"] == "CONFIRMED"
    assert replay["confirmed_at"] == seller_confirm["confirmed_at"]


def test_the_two_parties_may_confirm_in_either_order(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The seller may sign first; the trade has no notion of who wears the crown.

    Two signatures make an agreement, not a sequence of signatures. The seller answering "yes,
    those are the terms I will deliver against" before the buyer answers the mirror question is
    the same two words in the other order, and it must end on `CONFIRMED` either way.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = cod_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)

    seller_first = confirm(cod_client, seller, order_id)
    assert seller_first["status"] == "CREATED"

    buyer_second = confirm(cod_client, buyer, order_id)
    assert buyer_second["status"] == "CONFIRMED"
    assert buyer_second["confirmed_at"] is not None


def test_a_stranger_cannot_confirm(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    cod_sign_in: Callable[..., httpx.Response],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """A third account is not a party and gets the same 403 a third party always gets.

    The stranger signs in through the COD-enabled application itself, because that is the process
    whose loop will serve the confirmation — a token minted by another app's engine is workable,
    but it is exactly the cross-loop accident the COD fixtures exist to rule out.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = cod_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)

    verified = cod_sign_in("+251911000002")
    stranger = {"Authorization": f"Bearer {verified.json()['access_token']}"}
    verify = attempt_confirm(cod_client, stranger, order_id)

    assert verify.status_code == 403, verify.text
    error = verify.json()["error"]
    assert error["code"] == "FORBIDDEN"
    assert error["details"]["reason"] == "not_a_party_to_this_order"


def test_an_escrow_order_has_no_confirmation(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Money, not signatures, says yes on an escrow order.

    Confirmation is what stands in for funding, and an escrow order already has funding. Asking
    it to confirm is asking it to have two ways of being paid for, so instead of a route for that
    there is the same 409 the guard gives any order that cannot go where it is asked.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in_as_buyer()
    escrow = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=COD_BUY_KG)

    response = attempt_confirm(client, buyer, str(escrow["id"]))

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["order_id"] == escrow["id"]
    assert error["details"]["status"] == "AWAITING_FUNDING"
    assert error["details"]["requested"] == "CONFIRMED"


def test_a_cancelled_order_cannot_be_confirmed(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """A trade someone walked away from will not be re-signed behind their back.

    once a COD order is cancelled its word is spent; whether a party decides it liked the terms
    after all is a negotiation that starts again, not a confirmation the platform should pretend
    happened. The guard's 409 carries the order, its current status and the requested one.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = cod_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)
    cancelled = cod_client.post(
        f"/v1/orders/{order_id}/cancel",
        headers={**buyer, "Idempotency-Key": idempotency_key()},
    )
    assert cancelled.status_code == 200, cancelled.text

    response = attempt_confirm(cod_client, buyer, order_id)

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["order_id"] == order_id
    assert error["details"]["status"] == "CANCELLED"
    assert error["details"]["requested"] == "CONFIRMED"


def test_seller_delayed_only_after_seventy_two_hours_have_passed(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    backdate_confirmation: Callable[[str], None],
) -> None:
    """The flag is derived from `confirmed_at`, and the window starts when it is stamped.

    An order with no confirmation is never delayed — the seller has no agreement yet to be late
    on — and a confirmed order is delayed only once its three days are actually past. The
    boundary is drawn by rewriting the column the flag is measured from, exactly what the clock
    would have done to it, and the flag then flips on its own.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = cod_order(cod_client, seller, buyer, listing_body, order_body, idempotency_key)

    unconfirmed = cod_client.get(f"/v1/orders/{order_id}", headers=buyer).json()
    assert unconfirmed["confirmed_at"] is None
    assert unconfirmed["seller_delayed"] is False

    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    fresh = cod_client.get(f"/v1/orders/{order_id}", headers=buyer).json()
    assert fresh["confirmed_at"] is not None
    assert fresh["seller_delayed"] is False

    backdate_confirmation(order_id)
    elderly = cod_client.get(f"/v1/orders/{order_id}", headers=buyer).json()
    assert elderly["status"] == "CONFIRMED"
    assert elderly["seller_delayed"] is True


def test_an_escrow_order_reads_seller_delayed_false(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The delay flag has no business on an escrow order, and says None is not late.

    Escrow has its own enforcement — the 48-hour funding window answers a buyer who does not pay,
    and the seller's side of a delayed delivery is not something `seller_delayed` exists to
    name. An escrow order reads it `False` and never reads it anything else.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in_as_buyer()
    escrow = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=COD_BUY_KG)

    read = client.get(f"/v1/orders/{escrow['id']}", headers=buyer).json()

    assert read["payment_mode"] == "ESCROW"
    assert read["seller_delayed"] is False
    assert read["confirmed_at"] is None
