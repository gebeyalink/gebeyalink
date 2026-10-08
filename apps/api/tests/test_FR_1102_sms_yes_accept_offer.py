"""FR-1102, ``YES`` on an offer token: the buyer's answer to the seller's counter.

The negotiation alternates, so the party who may answer a counter is the one who did not propose
it: the buyer opens, the seller counters, and the buyer's YES closes it. What YES does not ask
for is the delivery address - a text message cannot collect it - so the address comes from the
buyer's own profile and the delivery method from the listing. This test holds the whole path:
the token is spent, the order is written with the counter's terms and the profile's address, and
the reply names the crop and the agreed price.
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

COUNTER_KG = 500
COUNTER_PRICE_SANTIM = 3_300_000


def _sign_in(client: TestClient, signed_in: Callable[..., dict[str, str]]) -> dict[str, str]:
    headers = signed_in(SELLER, language="en")
    updated = client.patch(
        "/v1/me",
        headers=headers,
        json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
    )
    assert updated.status_code == 200, updated.text
    return headers


def _sign_in_buyer(
    client: TestClient, signed_in_as_buyer: Callable[..., dict[str, str]]
) -> dict[str, str]:
    headers = signed_in_as_buyer(BUYER, language="en")
    updated = client.patch(
        "/v1/me",
        headers=headers,
        json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
    )
    assert updated.status_code == 200, updated.text
    return headers


def _countered_offer(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A listing, the buyer's opening bid and the seller's counter to answer."""
    created = client.post(
        "/v1/listings", headers=seller, json=listing_body(delivery_options=["PICKUP"])
    )
    assert created.status_code == 201, created.text
    listing = created.json()
    opening = client.post(f"/v1/listings/{listing['id']}/offers", headers=buyer, json=offer_body())
    assert opening.status_code == 201, opening.text
    counter = client.post(
        f"/v1/offers/{opening.json()['id']}/counter",
        headers=seller,
        json=offer_body(quantity_kg=COUNTER_KG, price_santim=COUNTER_PRICE_SANTIM),
    )
    assert counter.status_code == 200, counter.text
    return listing, counter.json()


def test_an_empty_kebele_falls_back_to_the_display_name(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    issue_action_token: Callable[..., str],
    sms_inbound: Callable[..., httpx.Response],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The third source in the ruling: the name they gave, when they named no place or firm."""

    seller = _sign_in(client, signed_in)
    buyer = signed_in_as_buyer(BUYER, language="en")
    updated = client.patch(
        "/v1/me",
        headers=buyer,
        json={"region_code": "OR", "woreda_id": 1, "display_name": "Aster Tesfaye"},
    )
    assert updated.status_code == 200, updated.text
    _, counter = _countered_offer(client, seller, buyer, listing_body, offer_body)
    token = issue_action_token(BUYER, f"offer_answer:{counter['id']}")

    sms_inbound(client, f"YES {token}", from_phone=BUYER)

    orders = stored_orders()
    assert len(orders) == 1
    assert orders[0]["buyer_address_text"] == "Aster Tesfaye"


def test_yes_accepts_the_counter_and_writes_the_order(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    issue_action_token: Callable[..., str],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    seller = _sign_in(client, signed_in)
    buyer = _sign_in_buyer(client, signed_in_as_buyer)
    _, counter = _countered_offer(client, seller, buyer, listing_body, offer_body)
    token = issue_action_token(BUYER, f"offer_answer:{counter['id']}")

    response = sms_inbound(client, f"YES {token}", from_phone=BUYER)
    assert response.status_code == 200
    assert response.content == b""
    assert (
        sms_recorder.messages_to(BUYER)[-1].body
        == "Your offer for Teff was accepted at 33000 birr/kg"
    )

    orders = stored_orders()
    assert len(orders) == 1
    order = orders[0]
    assert str(order["offer_id"]) == counter["id"]
    assert order["quantity_kg"] == COUNTER_KG
    assert order["unit_price_santim"] == COUNTER_PRICE_SANTIM
    assert order["delivery_method"] == "PICKUP"
    assert order["buyer_address_woreda_id"] == 1
    assert order["buyer_address_text"] == "Bole"
    assert order["status"] == "AWAITING_FUNDING"
    statuses = {str(row["id"]): row["status"] for row in stored_offers()}
    assert statuses[counter["id"]] == "ACCEPTED"
