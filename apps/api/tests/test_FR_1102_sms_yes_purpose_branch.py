"""FR-1102, which way ``YES`` goes: the token's prefix, and what the accept path still needs.

A YES is one word for two different acts, and the token's prefix is what decides between them:
``offer_answer`` accepts an offer, ``delivery_confirm`` confirms a delivery, and anything else is
refused. The accept path is also the one command that needs more than the message carries - a
delivery address the text channel cannot ask for - so when the buyer's own profile has no woreda
or no address, YES says which one is missing rather than half-creating an order.

The address is taken from the profile in the order a person would expect: the kebele they live
in, else the business they trade under, else the name they gave. Only when all three are empty is
there nothing to put on the order.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.providers.sms import RecordingSmsProvider

pytestmark = pytest.mark.integration

SELLER = "+251911000001"
BUYER = "+251911000003"


def _counter_with_token(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    issue_action_token: Callable[..., str],
) -> tuple[dict[str, Any], str]:
    """A listing, a buyer's bid, the seller's counter, and a token to answer it."""
    created = client.post(
        "/v1/listings", headers=seller, json=listing_body(delivery_options=["PICKUP"])
    )
    assert created.status_code == 201, created.text
    listing = created.json()
    opening = client.post(f"/v1/listings/{listing['id']}/offers", headers=buyer, json=offer_body())
    assert opening.status_code == 201, opening.text
    counter = client.post(
        f"/v1/offers/{opening.json()['id']}/counter", headers=seller, json=offer_body()
    )
    assert counter.status_code == 200, counter.text
    counter_body = counter.json()
    token = issue_action_token(BUYER, f"offer_answer:{counter_body['id']}")
    return counter_body, token


def test_a_token_with_no_recognised_prefix_is_refused(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    issue_action_token: Callable[..., str],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    signed_in(BUYER, language="en")
    token = issue_action_token(BUYER, "password_reset:00000000-0000-0000-0000-000000000000")
    sms_inbound(client, f"YES {token}", from_phone=BUYER)
    assert sms_recorder.messages_to(BUYER)[-1].body == f"That action code did not work: {token}"


def test_accept_without_a_woreda_asks_for_one(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    issue_action_token: Callable[..., str],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    seller = signed_in(SELLER, language="en")
    buyer = signed_in_as_buyer(BUYER, language="en")
    _, token = _counter_with_token(
        client, seller, buyer, listing_body, offer_body, issue_action_token
    )

    sms_inbound(client, f"YES {token}", from_phone=BUYER)
    assert (
        sms_recorder.messages_to(BUYER)[-1].body
        == "Your profile is not complete for an order: woreda"
    )
    assert stored_orders() == []


def test_accept_without_an_address_asks_for_one(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    issue_action_token: Callable[..., str],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    seller = signed_in(SELLER, language="en")
    buyer = signed_in_as_buyer(BUYER, language="en")
    updated = client.patch("/v1/me", headers=buyer, json={"region_code": "OR", "woreda_id": 1})
    assert updated.status_code == 200, updated.text
    _, token = _counter_with_token(
        client, seller, buyer, listing_body, offer_body, issue_action_token
    )

    sms_inbound(client, f"YES {token}", from_phone=BUYER)
    assert (
        sms_recorder.messages_to(BUYER)[-1].body
        == "Your profile is not complete for an order: address"
    )
    assert stored_orders() == []


def test_an_empty_kebele_falls_back_to_the_business_name(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    issue_action_token: Callable[..., str],
    sms_inbound: Callable[..., httpx.Response],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    seller = signed_in(SELLER, language="en")
    buyer = signed_in_as_buyer(BUYER, language="en")
    updated = client.patch(
        "/v1/me",
        headers=buyer,
        json={"region_code": "OR", "woreda_id": 1, "business_name": "Tesfaye Produce"},
    )
    assert updated.status_code == 200, updated.text
    _, token = _counter_with_token(
        client, seller, buyer, listing_body, offer_body, issue_action_token
    )

    sms_inbound(client, f"YES {token}", from_phone=BUYER)
    assert len(stored_orders()) == 1
    assert stored_orders()[0]["buyer_address_text"] == "Tesfaye Produce"


def test_an_offer_token_whose_offer_is_already_closed_is_refused(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    issue_action_token: Callable[..., str],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """The target is real but no longer open, so the accept path refuses and nothing changes."""

    seller = signed_in(SELLER, language="en")
    buyer = signed_in_as_buyer(BUYER, language="en")
    updated = client.patch(
        "/v1/me",
        headers=buyer,
        json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
    )
    assert updated.status_code == 200, updated.text
    created = client.post(
        "/v1/listings", headers=seller, json=listing_body(delivery_options=["PICKUP"])
    )
    assert created.status_code == 201, created.text
    offer = client.post(
        f"/v1/listings/{created.json()['id']}/offers", headers=buyer, json=offer_body()
    )
    assert offer.status_code == 201, offer.text
    offer_id = str(offer.json()["id"])
    refused = client.post(f"/v1/offers/{offer_id}/reject", headers=seller)
    assert refused.status_code == 200, refused.text

    token = issue_action_token(BUYER, f"offer_answer:{offer_id}")
    sms_inbound(client, f"YES {token}", from_phone=BUYER)

    assert sms_recorder.messages_to(BUYER)[-1].body == f"That action code did not work: {token}"
    assert [row["status"] for row in stored_offers()] == ["REJECTED"]


def test_a_delivery_token_on_an_unshipped_order_is_refused(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    issue_action_token: Callable[..., str],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The confirm path is reached, and the order is not shipped, so the token buys nothing."""

    seller = signed_in(SELLER, language="en")
    buyer = signed_in_as_buyer(BUYER, language="en")
    created = client.post("/v1/listings", headers=seller, json=listing_body())
    assert created.status_code == 201, created.text
    offer = client.post(
        f"/v1/listings/{created.json()['id']}/offers", headers=buyer, json=offer_body()
    )
    assert offer.status_code == 201, offer.text
    accepted = client.post(
        f"/v1/offers/{offer.json()['id']}/accept",
        headers={**seller, "Idempotency-Key": idempotency_key()},
        json={
            "delivery_method": "PICKUP",
            "buyer_address_woreda_id": 1,
            "buyer_address_text": "Bole",
        },
    )
    assert accepted.status_code == 201, accepted.text
    order_id = accepted.json()["id"]

    token = issue_action_token(BUYER, f"delivery_confirm:{order_id}")
    sms_inbound(client, f"YES {token}", from_phone=BUYER)
    assert sms_recorder.messages_to(BUYER)[-1].body == f"That action code did not work: {token}"
    assert stored_orders()[0]["status"] == "AWAITING_FUNDING"
