"""FR-1103, the offers on a seller's own listing, answered from the handset.

A seller reads the offers on one of their active listings and answers one, all as screens
under the listings path. The gate on Path 1 is in front of the whole menu, so this journey
also proves that a trader on an account with listings sees them, and that the accept is the
same accept the app runs: the offer row moves to ``ACCEPTED`` and an order row follows.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

SELLER = "+251911000001"
BUYER = "+251911000003"
SESSION_ID = "ses-ofr-1"


def _sign_in_seller(client: TestClient, signed_in: Callable[..., dict[str, str]]) -> dict[str, str]:
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


def _listing_with_a_buyer_offer(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> str:
    created = client.post(
        "/v1/listings", headers=seller, json=listing_body(delivery_options=["PICKUP"])
    )
    assert created.status_code == 201, created.text
    listing_id = str(created.json()["id"])
    placed = client.post(f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body())
    assert placed.status_code == 201, placed.text
    return listing_id


def test_a_seller_walks_to_an_offer_and_accepts_it(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    ussd_session: Callable[..., httpx.Response],
    stored_offers: Callable[[], list[dict[str, object]]],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """Listings, the row for the listing, the offer row, and the accept - one screen each."""

    seller = _sign_in_seller(client, signed_in)
    buyer = _sign_in_buyer(client, signed_in_as_buyer)
    listing_id = _listing_with_a_buyer_offer(client, seller, buyer, listing_body, offer_body)

    opened = ussd_session(client, "", session_id=SESSION_ID, phone_number=SELLER)
    assert opened.text == (
        "CON 1. My Listings\n2. Browse Crops\n3. My Orders\n4. Today's Prices\n5. Help"
    )

    listings = ussd_session(client, "1", session_id=SESSION_ID, phone_number=SELLER)
    assert listings.text == "CON 1. New listing\n2. Active (1)\n3. Paused (0)"

    active = ussd_session(client, "2", session_id=SESSION_ID, phone_number=SELLER)
    assert active.text.startswith("CON Your active listings:\n1. ")
    assert " Teff 2,000kg @32500 ETB" in active.text

    offers = ussd_session(client, "1", session_id=SESSION_ID, phone_number=SELLER)
    assert offers.text.startswith("CON Offers on ")
    assert offers.text.endswith(":\n1. 500kg @33000 ETB/kg")

    actions = ussd_session(client, "1", session_id=SESSION_ID, phone_number=SELLER)
    assert actions.text == "CON Reply 1 accept 2 reject 3 counter"

    accepted = ussd_session(client, "1", session_id=SESSION_ID, phone_number=SELLER)
    assert accepted.text == "END Offer accepted. An order will follow."

    offers_before = stored_offers()
    assert len(offers_before) == 1
    assert offers_before[0]["status"] == "ACCEPTED"
    orders = stored_orders()
    assert len(orders) == 1
    assert orders[0]["status"] == "AWAITING_FUNDING"
    assert str(orders[0]["listing_id"]) == listing_id


def test_a_replayed_accept_creates_one_order_not_two(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    ussd_session: Callable[..., httpx.Response],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The batch key around the accept means a gateway retry settles exactly one order."""

    seller = _sign_in_seller(client, signed_in)
    buyer = _sign_in_buyer(client, signed_in_as_buyer)
    _listing_with_a_buyer_offer(client, seller, buyer, listing_body, offer_body)

    for keypress in ["", "1", "2", "1", "1"]:
        ussd_session(client, keypress, session_id=SESSION_ID, phone_number=SELLER)
    accepted = ussd_session(client, "1", session_id=SESSION_ID, phone_number=SELLER)
    assert accepted.text == "END Offer accepted. An order will follow."
    again = ussd_session(client, "1", session_id=SESSION_ID, phone_number=SELLER)
    assert again.text == accepted.text

    orders = stored_orders()
    assert len(orders) == 1
