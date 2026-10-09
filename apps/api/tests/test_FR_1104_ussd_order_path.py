"""FR-1104, orders on the handset: the waiting group first, the empty groups honest.

The orders path is one menu of three groups, and an offer accepted on the listings path
places its order in the group that owns it - awaiting funding, because money is a later
slice. This file watches that order land in the awaiting group, and then walks the two
groups that are, for this trader, empty, proving each is told so rather than confused with
another.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

SELLER = "+251911000001"
BUYER = "+251911000003"
SESSION_ID = "ses-ord-1"
ORDER_WALK_SESSION = "ses-ord-2"


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


def _order_awaiting_funding(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    ussd_session: Callable[..., httpx.Response],
) -> None:
    """Reach the accepted screen the way the offer-path test does it."""
    created = client.post(
        "/v1/listings", headers=seller, json=listing_body(delivery_options=["PICKUP"])
    )
    assert created.status_code == 201, created.text
    placed = client.post(
        f"/v1/listings/{created.json()['id']}/offers", headers=buyer, json=offer_body()
    )
    assert placed.status_code == 201, placed.text
    for keypress in ["", "1", "2", "1", "1", "1"]:
        ussd_session(client, keypress, session_id=SESSION_ID, phone_number=SELLER)


def test_an_accepted_offer_lands_in_the_awaiting_group_and_out_of_the_rest(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    ussd_session: Callable[..., httpx.Response],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The three groups are three screens, and only one of them names the order."""

    seller = _sign_in_seller(client, signed_in)
    buyer = _sign_in_buyer(client, signed_in_as_buyer)
    _order_awaiting_funding(client, seller, buyer, listing_body, offer_body, ussd_session)

    orders = stored_orders()
    assert len(orders) == 1
    order_code = orders[0]["short_code"]
    assert orders[0]["status"] == "AWAITING_FUNDING"

    # The accept already ended one session at its terminal screen; orders are walked by dialling
    # a fresh session on the same number.
    opened = ussd_session(client, "", session_id=ORDER_WALK_SESSION, phone_number=SELLER)
    assert opened.text == (
        "CON 1. My Listings\n2. Browse Crops\n3. My Orders\n4. Today's Prices\n5. Help"
    )

    menu = ussd_session(client, "3", session_id=ORDER_WALK_SESSION, phone_number=SELLER)
    assert menu.text == "CON 1. Awaiting funding\n2. In progress\n3. Completed"

    awaiting = ussd_session(client, "1", session_id=ORDER_WALK_SESSION, phone_number=SELLER)
    assert awaiting.text == (f"CON Awaiting funding:\n1. {order_code} 500kg AWAITING_FUNDING")

    # Back to the menu, then into a group that has nothing in it for this trader.
    menu_again = ussd_session(client, "0", session_id=ORDER_WALK_SESSION, phone_number=SELLER)
    assert menu_again.text == "CON 1. Awaiting funding\n2. In progress\n3. Completed"
    in_progress = ussd_session(client, "2", session_id=ORDER_WALK_SESSION, phone_number=SELLER)
    assert in_progress.text == "CON In progress:\nYou have no orders here."

    back = ussd_session(client, "0", session_id=ORDER_WALK_SESSION, phone_number=SELLER)
    assert back.text == "CON 1. Awaiting funding\n2. In progress\n3. Completed"
    completed = ussd_session(client, "3", session_id=ORDER_WALK_SESSION, phone_number=SELLER)
    assert completed.text == "CON Completed:\nYou have no orders here."
