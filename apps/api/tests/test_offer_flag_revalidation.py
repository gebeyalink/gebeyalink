"""Accepting a bid: the payment mode is revalidated against the flags that exist *now*.

A bid chooses its mode when the thread opens, and a deployment's flags can move between the bid
and the answer. The rule is that acceptance re-reads them: a mode that was funded when the bid
was placed and is not funded when the seller says yes is refused by name, with no order written,
and the offer is left where it was because a configuration change is not the buyer's mistake.

The flags are fixed when an application is built, so a test that needs them to differ across the
two moves is a test of more than one application. These four tests use `mode_clients`, four
applications over one database and one signing key - one for each combination of the two flags -
so the offer is genuinely placed under one deployment and genuinely answered under another. The
four cases are the matrix: each flag alone, both at once, and the positive control where nothing
moved.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest

# SPEC A20: this file is integration - it drives real applications over this run's PostgreSQL and
# Redis, and a mocked flag would not tell us whether the rule holds at the seam it lives on.
pytestmark = pytest.mark.integration

SELLER = "+251911000031"
BUYER = "+251911000033"


async def _placed_offer(
    client: httpx.AsyncClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    *,
    payment_mode: str,
) -> str:
    """A listing under `client`, and the bid the buyer places on it with the mode asked for."""
    posted = await client.post(
        "/v1/listings", headers=seller, json=listing_body(delivery_options=["PICKUP"])
    )
    assert posted.status_code == 201, posted.text
    listing_id = str(posted.json()["id"])
    placed = await client.post(
        f"/v1/listings/{listing_id}/offers",
        headers=buyer,
        json=offer_body(payment_mode=payment_mode),
    )
    assert placed.status_code == 201, placed.text
    return str(placed.json()["id"])


async def _accept(
    client: httpx.AsyncClient,
    seller: dict[str, str],
    offer_id: str,
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> httpx.Response:
    response = await client.post(
        f"/v1/offers/{offer_id}/accept",
        headers={**seller, "Idempotency-Key": idempotency_key()},
        json=order_body(),
    )
    assert isinstance(response, httpx.Response)
    return response


async def test_an_escrow_bid_is_refused_when_escrow_is_off_by_acceptance(
    mode_clients: dict[str, httpx.AsyncClient],
    sign_in_over: Callable[..., Awaitable[dict[str, str]]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_offers: Callable[[], list[dict[str, object]]],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """An escrow bid placed with both modes open is refused once escrow alone has closed.

    The bid was placed by a deployment that funded both modes, so escrow was a real choice, not
    the only option left standing. By acceptance the deployment funds COD only, and escrow is
    refused by name. The offer stays `OPEN` and no order is written.
    """
    both = mode_clients["both"]
    cod_only = mode_clients["cod"]
    seller = await sign_in_over(both, SELLER)
    buyer = await sign_in_over(both, BUYER)
    offer_id = await _placed_offer(
        both, seller, buyer, listing_body, offer_body, payment_mode="ESCROW"
    )

    refused = await _accept(cod_only, seller, offer_id, order_body, idempotency_key)

    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == {"feature": "escrow"}
    assert [row["status"] for row in stored_offers()] == ["OPEN"]
    assert stored_orders() == []


async def test_a_cod_bid_is_refused_when_cod_is_off_by_acceptance(
    mode_clients: dict[str, httpx.AsyncClient],
    sign_in_over: Callable[..., Awaitable[dict[str, str]]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_offers: Callable[[], list[dict[str, object]]],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The other half of the switch: a COD bid refused once COD alone has closed."""
    both = mode_clients["both"]
    escrow_only = mode_clients["escrow"]
    seller = await sign_in_over(both, SELLER)
    buyer = await sign_in_over(both, BUYER)
    offer_id = await _placed_offer(
        both, seller, buyer, listing_body, offer_body, payment_mode="COD"
    )

    refused = await _accept(escrow_only, seller, offer_id, order_body, idempotency_key)

    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == {"feature": "cod"}
    assert [row["status"] for row in stored_offers()] == ["OPEN"]
    assert stored_orders() == []


async def test_a_bid_is_refused_when_both_modes_have_closed(
    mode_clients: dict[str, httpx.AsyncClient],
    sign_in_over: Callable[..., Awaitable[dict[str, str]]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_offers: Callable[[], list[dict[str, object]]],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """A bid placed with both modes open is refused when the deployment funds neither.

    A deployment that has opened nothing at all. The refusal is the same shape as the single-flag
    ones, which is the point: the rule does not depend on one flag being on to report the other
    being off.
    """
    both = mode_clients["both"]
    none = mode_clients["none"]
    seller = await sign_in_over(both, SELLER)
    buyer = await sign_in_over(both, BUYER)
    offer_id = await _placed_offer(
        both, seller, buyer, listing_body, offer_body, payment_mode="ESCROW"
    )

    refused = await _accept(none, seller, offer_id, order_body, idempotency_key)

    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == {"feature": "escrow"}
    assert [row["status"] for row in stored_offers()] == ["OPEN"]
    assert stored_orders() == []


async def test_a_bid_placed_with_both_modes_open_is_accepted_with_both_open(
    mode_clients: dict[str, httpx.AsyncClient],
    sign_in_over: Callable[..., Awaitable[dict[str, str]]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_offers: Callable[[], list[dict[str, object]]],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The positive control: nothing moved, so the escrow bid becomes an escrow order.

    Without this, the three refusals above would be indistinguishable from a rule that refuses
    everything. The mode travels from the offer onto the order, and the offer is the one that
    moved.
    """
    both = mode_clients["both"]
    seller = await sign_in_over(both, SELLER)
    buyer = await sign_in_over(both, BUYER)
    offer_id = await _placed_offer(
        both, seller, buyer, listing_body, offer_body, payment_mode="ESCROW"
    )

    accepted = await _accept(both, seller, offer_id, order_body, idempotency_key)

    assert accepted.status_code == 201, accepted.text
    order = accepted.json()
    assert order["offer_id"] == offer_id
    assert order["payment_mode"] == "ESCROW"
    assert order["status"] == "AWAITING_FUNDING"
    assert stored_orders()[0]["payment_mode"] == "ESCROW"
    assert [row["status"] for row in stored_offers() if str(row["id"]) == offer_id] == ["ACCEPTED"]
