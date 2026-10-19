"""FR-1101, a session from first press to the offer it places: one keypress, one screen.

This is the whole loop the engine runs against the real application: a registered trader
dialls, chooses a language they have already saved to their account, walks the browse path
by region and crop, reads a result the seller posted, and places an offer, all as plain-text
``CON`` screens. The screens are asserted line for line, not as substrings of themselves,
because the USSD channel is data and a drifted sentence is a broken screen.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

SELLER = "+251911000001"
BUYER = "+251911000003"
SESSION_ID = "ses-life-1"


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


def test_a_registered_dial_walks_the_browse_path_and_places_an_offer(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    ussd_session: Callable[..., httpx.Response],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """The browse path, gate and all: region, crop, result, detail, terms, confirm, sent."""

    seller = _sign_in_seller(client, signed_in)
    created = client.post(
        "/v1/listings", headers=seller, json=listing_body(delivery_options=["PICKUP"])
    )
    assert created.status_code == 201, created.text
    _sign_in_buyer(client, signed_in_as_buyer)

    # The first press of a fresh dial is the main menu: the number is on an account.
    opened = ussd_session(client, "", session_id=SESSION_ID, phone_number=BUYER)
    assert opened.status_code == 200
    assert opened.text == (
        "CON 1. My Listings\n2. Browse Crops\n3. My Orders\n4. Today's Prices\n5. Help"
    )

    # Path 2: browse by region, then by crop.
    regions = ussd_session(client, "2", session_id=SESSION_ID, phone_number=BUYER)
    assert regions.text.startswith("CON Last used: Oromia\nChoose a region:\n1. Addis Ababa")
    assert regions.text.endswith("\n13. Tigray")

    crops = ussd_session(client, "8", session_id=SESSION_ID, phone_number=BUYER)
    assert crops.text == "CON Choose a crop:\n1. Teff\n2. Wheat\n3. More"

    # One result page, with the seller's row and nothing else out of place.
    results = ussd_session(client, "1", session_id=SESSION_ID, phone_number=BUYER)
    assert results.text.startswith("CON 1. ")
    assert "Teff 2,000kg @32500 ETB — Adaa" in results.text

    # The detail screen offers the next step; the gate lets a registered handset take it.
    detail = ussd_session(client, "1", session_id=SESSION_ID, phone_number=BUYER)
    assert detail.text.startswith("CON Teff 2,000kg @32500 ETB — Adaa\n1. Make offer")
    quantity = ussd_session(client, "1", session_id=SESSION_ID, phone_number=BUYER)
    assert quantity.text == "CON Quantity in kg?"
    price = ussd_session(client, "500", session_id=SESSION_ID, phone_number=BUYER)
    assert price.text == "CON Price per kg in ETB?"
    confirm = ussd_session(client, "30", session_id=SESSION_ID, phone_number=BUYER)
    assert confirm.text == "CON Offer 500kg @30 ETB/kg?\n1. Confirm"
    sent = ussd_session(client, "1", session_id=SESSION_ID, phone_number=BUYER)
    assert sent.text.startswith("END Offer sent: ")

    offers = stored_offers()
    assert len(offers) == 1
    assert offers[0]["quantity_kg"] == 500
    assert offers[0]["price_santim"] == 3000
    assert offers[0]["status"] == "OPEN"
    assert "buyer_id" in offers[0]


def test_a_replayed_press_on_the_ended_session_places_no_second_offer(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    ussd_session: Callable[..., httpx.Response],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """An offer is placed once even if the gateway re-posts the last press."""

    seller = _sign_in_seller(client, signed_in)
    created = client.post(
        "/v1/listings", headers=seller, json=listing_body(delivery_options=["PICKUP"])
    )
    assert created.status_code == 201, created.text
    _sign_in_buyer(client, signed_in_as_buyer)

    for keypress in ["", "2", "8", "1", "1", "1", "500", "30"]:
        ussd_session(client, keypress, session_id=SESSION_ID, phone_number=BUYER)
    sent = ussd_session(client, "1", session_id=SESSION_ID, phone_number=BUYER)
    assert sent.text.startswith("END Offer sent: ")
    # A gateway retry arrives on the ended session: it must re-render, not re-place.
    again = ussd_session(client, "1", session_id=SESSION_ID, phone_number=BUYER)
    assert again.text == sent.text

    offers = stored_offers()
    assert len(offers) == 1
