"""The feature-phone channels when the deployment funds no payment mode.

A feature-phone trader has nowhere to say how a sale would be paid for, so the SMS and USSD
adapters choose on the trader's behalf. When escrow is off the adapter sends `COD`; if the
deployment has opened nothing at all, the mode the adapter sends is itself refused by the domain
- the adapter is a convenience, not the enforcement point (`adapter_offer_mode`). These tests
hold that second line: on both channels the refusal reaches the trader as the channel's generic
reply (the help menu on SMS, the invalid-choice screen on USSD), and it writes no offer and no
order.

The accept side is here too: the one USSD accept move the earlier slice's survey called out is
driven again by a refusal branch rather than the happy path, because the happy path is already
proven in the offer-path file. What this file adds is a seller walking to an offer whose buyer
has no address on file, and the refusal that produces.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.providers.sms import RecordingSmsProvider

pytestmark = pytest.mark.integration

SELLER = "+251911000041"
BUYER = "+251911000043"

SESSION_ID = "ses-w1a-1"


def _signed_in_seller(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> dict[str, str]:
    """A seller signed in and given the pilot region, so a listing can be posted."""
    headers = signed_in(SELLER, language="en")
    updated = client.patch(
        "/v1/me",
        headers=headers,
        json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
    )
    assert updated.status_code == 200, updated.text
    return headers


def _listing(
    client: TestClient,
    seller: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
) -> dict[str, object]:
    created = client.post("/v1/listings", headers=seller, json=listing_body())
    assert created.status_code == 201, created.text
    body: dict[str, object] = created.json()
    return body


def _walk_to_the_offer(
    client: TestClient,
    ussd_session: Callable[..., httpx.Response],
) -> httpx.Response:
    """Press through the browse path to the offer's confirm step and take it."""
    for keypress in ["", "2", "8", "1", "1", "1", "500", "30"]:
        ussd_session(client, keypress, session_id=SESSION_ID, phone_number=BUYER)
    sent: httpx.Response = ussd_session(client, "1", session_id=SESSION_ID, phone_number=BUYER)
    return sent


def test_an_sms_offer_is_answered_with_help_when_no_mode_is_funded(
    no_modes_client: TestClient,
    no_modes_signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
    stored_offers: Callable[[], list[dict[str, object]]],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """Behaviour A on SMS: the adapter sends COD, the domain refuses, the menu answers.

    A texted `OFFER` reaches the domain carrying cash-on-delivery, exactly as it does on the
    COD-funded deployment - and is refused there by the flag this deployment did not set. The
    sender hears the generic help menu, not a sales pitch and not an error code, and nothing is
    written.
    """
    seller = _signed_in_seller(no_modes_client, no_modes_signed_in)
    listing = _listing(no_modes_client, seller, listing_body)
    no_modes_signed_in(BUYER, language="en")

    response = sms_inbound(
        no_modes_client, f"OFFER {listing['short_code']} 500 33", from_phone=BUYER
    )

    assert response.status_code == 200, response.text
    assert response.content == b""
    assert sms_recorder.messages_to(BUYER)[-1].body == (
        "Help: PRICE POST OFFER STATUS CANCEL PAYOUTS. USSD *851#"
    )
    assert stored_offers() == []
    assert stored_orders() == []


def test_a_ussd_offer_is_refused_when_no_mode_is_funded(
    no_modes_client: TestClient,
    no_modes_signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    ussd_session: Callable[..., httpx.Response],
    stored_offers: Callable[[], list[dict[str, object]]],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """Behaviour A on USSD: the same refusal, as the invalid-choice screen.

    The offer tree has no screen for a payment mode, so the adapter sends COD and the domain
    refuses it; the engine's generic refusal is what the handset sees. The refusal marker on the
    screen is what distinguishes this from the confirm screen the offer would otherwise land on.
    """
    seller = _signed_in_seller(no_modes_client, no_modes_signed_in)
    _listing(no_modes_client, seller, listing_body)
    no_modes_signed_in(BUYER)

    sent = _walk_to_the_offer(no_modes_client, ussd_session)

    assert sent.text.startswith("CON ")
    assert "❌ " in sent.text
    assert stored_offers() == []
    assert stored_orders() == []


def test_a_ussd_accept_is_refused_for_a_buyer_with_no_address_on_file(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    ussd_session: Callable[..., httpx.Response],
    stored_offers: Callable[[], list[dict[str, object]]],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The USSD accept refuses before writing when the buyer has no woreda to deliver to.

    The accept path builds the delivery address from the buyer's profile, and a buyer who has
    never set a woreda has no address to build. The seller walking to the accept learns it as the
    invalid-choice screen, the offer stays `OPEN`, and no order is written - a refusal, not the
    loss of the negotiation.
    """
    seller = _signed_in_seller(client, signed_in)
    # Deliberately no profile patch: this buyer has no address on file.
    buyer = signed_in_as_buyer(BUYER, language="en")
    listing = _listing(client, seller, listing_body)
    placed = client.post(f"/v1/listings/{listing['id']}/offers", headers=buyer, json=offer_body())
    assert placed.status_code == 201, placed.text

    for keypress in ["", "1", "2", "1", "1"]:
        ussd_session(client, keypress, session_id=SESSION_ID, phone_number=SELLER)
    refused = ussd_session(client, "1", session_id=SESSION_ID, phone_number=SELLER)

    assert refused.text.startswith("CON ")
    assert "❌ " in refused.text
    assert stored_orders() == []
    assert [row["status"] for row in stored_offers()] == ["OPEN"]
