"""One `Idempotency-Key`, one effect, whichever of the two people tapping meant it.

The header exists because of a slow connection, not because of a distributed transaction. A buyer
whose request is taking so long that they tap it again has meant one purchase, and answering that
with a second order would not be a duplicate so much as a second sale of stock that has already
been sold.

The properties are separate and each is a decision rather than an implementation detail, so each
has its own test below. A retry under the same key is answered with the stored answer and does no
work. A key reused for a *different* request is refused, because handing back the first order to a
client that asked for a different one is a way to buy the wrong quantity and be told it is right.
A failure hands the key back, because idempotency is not a punishment for a typo. And two requests
genuinely in flight at once get one answer and one refusal, rather than one of them being made to
wait on a transaction it cannot see the end of.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest

from apps.api.models.listing import Listing
from apps.api.models.order import Order
from apps.api.services.order_service import OrderService

# SPEC A20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

LISTING_KG = 1_000
BUY_KG = 400

Signer = Callable[..., Awaitable[dict[str, str]]]


def test_the_same_key_under_a_retry_answers_with_the_first_answer_and_does_no_work(
    client: httpx.Client,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """A buyer tapping twice, and the second tap getting the first tap's order back.

    The important half is what is *not* here: one order, one reservation, and one deducted from
    the listing's availability. A replay that returned the same JSON while creating a second order
    would pass an assertion on the body alone, which is why the rows are read back here.
    """
    seller = signed_in()
    listing = client.post("/v1/listings", headers=seller, json=listing_body(quantity_kg=LISTING_KG))
    assert listing.status_code == 201, listing.text
    buyer = signed_in("+251911000003")
    key = idempotency_key()
    headers = {**buyer, "Idempotency-Key": key}

    first = client.post(
        f"/v1/listings/{listing.json()['id']}/buy-now",
        headers=headers,
        json=order_body(quantity_kg=BUY_KG),
    )
    second = client.post(
        f"/v1/listings/{listing.json()['id']}/buy-now",
        headers=headers,
        json=order_body(quantity_kg=BUY_KG),
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json() == first.json()
    assert len(stored_orders()) == 1
    assert len(stored_reservations()) == 1
    after = client.get(f"/v1/listings/{listing.json()['id']}", headers=buyer)
    assert after.json()["available_kg"] == LISTING_KG - BUY_KG


def test_one_key_reused_for_a_different_request_is_refused_rather_than_answered(
    client: httpx.Client,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """A key is one intent, and two intents under one key is a client that will be told so.

    422 with the header named, rather than the first order handed back as though it had answered
    the second question: without the fingerprint, idempotency would be the most reliable way in the
    product to buy the wrong quantity and never find out.
    """
    seller = signed_in()
    listing = client.post("/v1/listings", headers=seller, json=listing_body(quantity_kg=LISTING_KG))
    assert listing.status_code == 201, listing.text
    buyer = signed_in("+251911000003")
    key = idempotency_key()
    headers = {**buyer, "Idempotency-Key": key}

    first = client.post(
        f"/v1/listings/{listing.json()['id']}/buy-now",
        headers=headers,
        json=order_body(quantity_kg=BUY_KG),
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/v1/listings/{listing.json()['id']}/buy-now",
        headers=headers,
        json=order_body(quantity_kg=BUY_KG + 100),
    )

    assert second.status_code == 422, second.text
    error = second.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"] == {"idempotency_key": "already_used_for_a_different_request"}
    assert len(stored_orders()) == 1


def test_a_key_that_failed_gives_itself_back_so_the_correction_can_retry_under_it(
    client: httpx.Client,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """A refused request must not spend its key for a day.

    The client got the weight wrong, was told what is available, and sends the corrected weight
    under the same key because it is the same intent - the one they meant all along. Caching the
    failure would make that impossible: the corrected request would be answered with the refusal
    it is meant to fix, for twenty-four hours, and the buyer would conclude the platform was
    broken rather than that they had typed a number the platform did not have.
    """
    seller = signed_in()
    listing = client.post("/v1/listings", headers=seller, json=listing_body(quantity_kg=LISTING_KG))
    assert listing.status_code == 201, listing.text
    buyer = signed_in("+251911000003")
    key = idempotency_key()
    headers = {**buyer, "Idempotency-Key": key}

    too_many = client.post(
        f"/v1/listings/{listing.json()['id']}/buy-now",
        headers=headers,
        json=order_body(quantity_kg=LISTING_KG + 1),
    )
    corrected = client.post(
        f"/v1/listings/{listing.json()['id']}/buy-now",
        headers=headers,
        json=order_body(quantity_kg=BUY_KG),
    )

    assert too_many.status_code == 422, too_many.text
    assert corrected.status_code == 201, corrected.text
    assert len(stored_orders()) == 1


def test_a_key_belongs_to_one_account_and_not_to_the_string(
    client: httpx.Client,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """Two buyers who happen to have chosen the same key are two buyers, not one intent.

    A UUID makes a collision between two handsets vanishingly unlikely, and this test is not about
    whether it can happen: it is about what *means* something if it does. Scoping the key to the
    account means a client that generates keys badly still cannot swallow somebody else's order,
    and a key that had been global would let one buyer cancel or shadow another's request.
    """
    seller = signed_in()
    listing = client.post("/v1/listings", headers=seller, json=listing_body(quantity_kg=LISTING_KG))
    assert listing.status_code == 201, listing.text
    first = signed_in("+251911000003")
    second = signed_in("+251911000004")
    key = idempotency_key()

    first_order = client.post(
        f"/v1/listings/{listing.json()['id']}/buy-now",
        headers={**first, "Idempotency-Key": key},
        json=order_body(quantity_kg=BUY_KG),
    )
    second_order = client.post(
        f"/v1/listings/{listing.json()['id']}/buy-now",
        headers={**second, "Idempotency-Key": key},
        json=order_body(quantity_kg=BUY_KG),
    )

    assert first_order.status_code == 201, first_order.text
    assert second_order.status_code == 201, second_order.text
    assert first_order.json()["id"] != second_order.json()["id"]
    assert len(stored_orders()) == 2


def test_a_malformed_key_is_refused_before_any_business_rule_is_consulted(
    client: httpx.Client,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
) -> None:
    """A key the platform cannot read is a key it cannot store under anything sensible.

    Declared as a UUID rather than as a string of some length, so the framework refuses it: storing
    a mangled key under an invented name would be worse than refusing it, because the retry would
    not match and the duplicate would go through.
    """
    seller = signed_in()
    listing = client.post("/v1/listings", headers=seller, json=listing_body(quantity_kg=LISTING_KG))
    assert listing.status_code == 201, listing.text
    buyer = signed_in("+251911000003")

    response = client.post(
        f"/v1/listings/{listing.json()['id']}/buy-now",
        headers={**buyer, "Idempotency-Key": "buy-this-once"},
        json=order_body(quantity_kg=BUY_KG),
    )

    assert response.status_code == 422, response.text


async def test_a_second_request_while_the_first_is_still_running_is_refused_not_queued(
    concurrent_client: httpx.AsyncClient,
    signed_in_concurrently: Signer,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """Two requests in flight under one key, and the second is told the first is still running.

    Neither made to wait: the second cannot see the end of the first transaction, so holding it
    would be holding it for an unbounded time on the strength of a guess. 409 with the reason
    spelled out is the answer that lets a client decide - wait and retry, or give up and try again
    later - rather than the answer that hides a deadlock until a user gives up.

    The first request is held at the moment it would commit, so both requests are genuinely in
    flight when the second arrives; without that, the second would usually arrive after the answer
    was stored and this test would be measuring the replay path instead of this one.
    """
    unpatched = OrderService._open_order
    inside = asyncio.Event()
    release = asyncio.Event()

    async def held(self: OrderService, listing: Listing, terms: object, **kwargs: object) -> Order:
        order = await unpatched(self, listing, terms, **kwargs)  # type: ignore[arg-type]
        inside.set()
        # Bounded, so a test that never gets here fails rather than hanging the suite.
        await asyncio.wait_for(release.wait(), timeout=30)
        return order

    monkeypatch.setattr(OrderService, "_open_order", held)
    http = concurrent_client
    seller = await signed_in_concurrently()
    created = await http.post(
        "/v1/listings", headers=seller, json=listing_body(quantity_kg=LISTING_KG)
    )
    assert created.status_code == 201, created.text
    buyer = await signed_in_concurrently("+251911000003")
    key = idempotency_key()
    headers = {**buyer, "Idempotency-Key": key}

    async def buy() -> httpx.Response:
        return await http.post(
            f"/v1/listings/{created.json()['id']}/buy-now",
            headers=headers,
            json=order_body(quantity_kg=BUY_KG),
        )

    first_task = asyncio.ensure_future(buy())
    await asyncio.wait_for(inside.wait(), timeout=30)
    second = await buy()
    release.set()
    first = await asyncio.wait_for(first_task, timeout=30)

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    error = second.json()["error"]
    assert error["code"] == "CONCURRENT_MODIFICATION"
    assert error["details"]["reason"] == "a_request_with_this_idempotency_key_is_still_running"
    assert len(stored_orders()) == 1
