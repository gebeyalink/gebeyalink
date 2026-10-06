"""What is left of a listing, and what happens when two people want the same of it.

FR-1901 puts availability in the reservation rows rather than on the listing, which means the
number can only be right if every sale reads it and writes it under the same lock. These tests
are about that lock, in both the shapes the specification asks for.

The first is AC-19.1: two bids of eighty kilos against a hundred, accepted one after the other.
The second one does not fit, and the specification is precise about how that is answered - 422
`OFFER_CONFLICT` with the available amount, the bid still `OPEN`, and the first order untouched.

The rest are the same situation with the two requests genuinely overlapping, which is where the
lock earns its keep. The rule there is different, and deliberately so: a bid that fitted a moment
ago and no longer does did not fail a check, it lost a race, and the answer is 409 so the client
looks again rather than giving up on a bid that was always affordable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest

from apps.api.models.listing import Listing
from apps.api.services.order_service import OrderService

# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

#: The listing both bids are measured against: a hundred kilos, and a minimum order low enough
#: that an eighty-kilo bid is a valid thing for a buyer to write.
LISTING_KG = 100
BID_KG = 80

Signer = Callable[..., Awaitable[dict[str, str]]]


async def post_listing(
    http: httpx.AsyncClient,
    headers: dict[str, str],
    body: Callable[..., dict[str, object]],
    **overrides: object,
) -> str:
    """Post a listing and hand back its identity."""
    response = await http.post("/v1/listings", headers=headers, json=body(**overrides))
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def place_offer(
    http: httpx.AsyncClient,
    headers: dict[str, str],
    listing_id: str,
    body: Callable[..., dict[str, object]],
    **overrides: object,
) -> str:
    """Place an offer and hand back its identity."""
    response = await http.post(
        f"/v1/listings/{listing_id}/offers", headers=headers, json=body(**overrides)
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def hold_both_at_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold every sale at the door of the listing lock until they have all arrived.

    Without this the two requests in a concurrency test are merely launched together, which is not
    the same as arriving together: whichever one the event loop happened to serve first could
    finish before the other had read anything, and the loser would then look like a bid that never
    fitted rather than one that lost. The barrier makes both requests decide what they want before
    either of them locks the row, which is exactly the situation the lock is for - and it makes
    the outcome of the test the same on a fast machine as on a slow one.

    Patched onto the one step where "what did I think was available?" is read and decided. The
    production code is untouched, and nothing else in the request is delayed.
    """
    unpatched = OrderService._reserved_before_lock
    both_arrived = asyncio.Barrier(2)

    async def gated(self: OrderService, listing: Listing) -> int:
        spoken_for = await unpatched(self, listing)
        # Bounded, so a request that never arrives cannot hang the suite: a barrier with one
        # waiter left raises instead of waiting for ever.
        await asyncio.wait_for(both_arrived.wait(), timeout=30)
        return spoken_for

    monkeypatch.setattr(OrderService, "_reserved_before_lock", gated)


async def test_the_second_of_two_bids_that_no_longer_fit_is_refused_and_the_first_is_untouched(
    concurrent_client: httpx.AsyncClient,
    signed_in_concurrently: Signer,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_offers: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """AC-19.1, in order: one order stands, the other bid keeps standing, and 422 says how much.

    The available amount in `details` is the point of the refusal rather than a courtesy: the
    seller can see at once that the bid needs twenty kilos fewer, or needs another listing.
    """
    http = concurrent_client
    seller = await signed_in_concurrently()
    listing_id = await post_listing(
        http, seller, listing_body, quantity_kg=LISTING_KG, min_order_kg=50
    )
    first_bid = await place_offer(
        http,
        await signed_in_concurrently("+251911000003"),
        listing_id,
        offer_body,
        quantity_kg=BID_KG,
    )
    second_bid = await place_offer(
        http,
        await signed_in_concurrently("+251911000004"),
        listing_id,
        offer_body,
        quantity_kg=BID_KG,
    )

    accepted = await http.post(
        f"/v1/offers/{first_bid}/accept",
        headers={**seller, "Idempotency-Key": idempotency_key()},
        json=order_body(),
    )
    refused = await http.post(
        f"/v1/offers/{second_bid}/accept",
        headers={**seller, "Idempotency-Key": idempotency_key()},
        json=order_body(),
    )

    assert accepted.status_code == 201, accepted.text
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "OFFER_CONFLICT"
    assert error["details"]["available"] == LISTING_KG - BID_KG
    assert error["details"]["requested_kg"] == BID_KG

    statuses = {str(row["id"]): row["status"] for row in stored_offers()}
    assert statuses[first_bid] == "ACCEPTED"
    assert statuses[second_bid] == "OPEN"

    orders = stored_orders()
    assert len(orders) == 1
    assert str(orders[0]["id"]) == accepted.json()["id"]
    assert orders[0]["status"] == "AWAITING_FUNDING"
    assert orders[0]["quantity_kg"] == BID_KG

    held = [row for row in stored_reservations() if row["state"] == "HELD"]
    assert len(held) == 1
    assert held[0]["quantity_kg"] == BID_KG

    listing = await http.get(f"/v1/listings/{listing_id}", headers=seller)
    assert listing.json()["available_kg"] == LISTING_KG - BID_KG


async def test_two_buyers_racing_for_the_last_kilos_produce_one_order_and_one_409(
    concurrent_client: httpx.AsyncClient,
    signed_in_concurrently: Signer,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """The whole of the last hundred kilos, twice, at the same moment.

    One 201 and one 409, and - the part that is easy to get wrong and matters more - one order row
    and one reservation. A lock that let two orders through would have guarded nothing, and a test
    that only looked at the status codes would not have noticed.
    """
    http = concurrent_client
    seller = await signed_in_concurrently()
    listing_id = await post_listing(
        http, seller, listing_body, quantity_kg=LISTING_KG, min_order_kg=50
    )
    first = await signed_in_concurrently("+251911000003")
    second = await signed_in_concurrently("+251911000004")
    hold_both_at_the_lock(monkeypatch)

    async def buy_now(headers: dict[str, str]) -> httpx.Response:
        return await http.post(
            f"/v1/listings/{listing_id}/buy-now",
            headers={**headers, "Idempotency-Key": idempotency_key()},
            json=order_body(quantity_kg=LISTING_KG),
        )

    responses = await asyncio.gather(buy_now(first), buy_now(second))

    codes = sorted(response.status_code for response in responses)
    assert codes == [201, 409], [response.text for response in responses]
    loser = next(response for response in responses if response.status_code == 409)
    assert loser.json()["error"]["code"] == "CONCURRENT_MODIFICATION"
    assert loser.json()["error"]["details"]["available"] == 0

    assert len(stored_orders()) == 1
    held = stored_reservations()
    assert len(held) == 1
    assert held[0]["quantity_kg"] == LISTING_KG
    assert held[0]["state"] == "HELD"

    listing = await http.get(f"/v1/listings/{listing_id}", headers=seller)
    assert listing.json()["available_kg"] == 0
    assert listing.json()["status"] == "SOLD_OUT"


async def test_two_acceptances_racing_for_the_last_kilos_leave_the_losing_bid_open(
    concurrent_client: httpx.AsyncClient,
    signed_in_concurrently: Signer,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """FR-1903's second half: racing accepts, one success, and the loser told to look again.

    AC-5.1 puts the loser's answer at 409 `CONCURRENT_MODIFICATION`, and that is not a technicality
    dressed up as a rule. 422 `OFFER_CONFLICT` says these terms were never possible for this
    listing; 409 says somebody else got there first and the bid may still be good once the buyer
    lowers it. The losing bid is left `OPEN` either way, so the seller is not left having thrown
    away a buyer because two of their own taps arrived together.
    """
    http = concurrent_client
    seller = await signed_in_concurrently()
    listing_id = await post_listing(
        http, seller, listing_body, quantity_kg=LISTING_KG, min_order_kg=50
    )
    first = await place_offer(
        http,
        await signed_in_concurrently("+251911000003"),
        listing_id,
        offer_body,
        quantity_kg=LISTING_KG,
    )
    second = await place_offer(
        http,
        await signed_in_concurrently("+251911000004"),
        listing_id,
        offer_body,
        quantity_kg=LISTING_KG,
    )
    hold_both_at_the_lock(monkeypatch)

    async def accept(offer_id: str) -> httpx.Response:
        return await http.post(
            f"/v1/offers/{offer_id}/accept",
            headers={**seller, "Idempotency-Key": idempotency_key()},
            json=order_body(),
        )

    responses = await asyncio.gather(accept(first), accept(second))

    codes = sorted(response.status_code for response in responses)
    assert codes == [201, 409], [response.text for response in responses]
    loser_offer = first if responses[0].status_code == 409 else second
    statuses = {str(row["id"]): row["status"] for row in stored_offers()}
    assert statuses[loser_offer] == "OPEN"
    assert sum(1 for status in statuses.values() if status == "ACCEPTED") == 1
    assert len(stored_orders()) == 1
