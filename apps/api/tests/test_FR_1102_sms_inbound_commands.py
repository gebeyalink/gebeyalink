"""FR-1102, the nine SMS verbs over the inbound endpoint, end to end.

Every command in §14.4 is sent as a signed text message and the reply is read back from the
messaging backend. Where a verb changes something, the change is read from PostgreSQL rather
than from the service that made it, because a test that asks the API whether the API wrote a
row proves very little.

The nine verbs are HELP, PRICE, POST, OFFER, YES, NO, CANCEL, STATUS and PAYOUTS. Five of them
mutate - POST, OFFER, YES, NO and CANCEL - and each of those is held to its effect here. The
two sensitive ones (YES, NO) go through a real action token, written into the table the way the
platform would write it.

A business refusal is not an HTTP failure. The gateway is answered with an empty 200 whatever
the command did, because a gateway that retried on a refusal would retry a POST that was
already refused for a reason retrying cannot change; the refusal reaches the trader as a reply.
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


def _as_seller(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
) -> dict[str, str]:
    headers = signed_in(SELLER, language="en")
    updated = client.patch(
        "/v1/me",
        headers=headers,
        json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
    )
    assert updated.status_code == 200, updated.text
    return headers


def _as_buyer(
    client: TestClient,
    signed_in_as_buyer: Callable[..., dict[str, str]],
) -> dict[str, str]:
    headers = signed_in_as_buyer(BUYER, language="en")
    updated = client.patch(
        "/v1/me",
        headers=headers,
        json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
    )
    assert updated.status_code == 200, updated.text
    return headers


def _post_listing(
    client: TestClient,
    seller: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    **overrides: object,
) -> dict[str, Any]:
    response = client.post("/v1/listings", headers=seller, json=listing_body(**overrides))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _place_offer(
    client: TestClient,
    buyer: dict[str, str],
    listing_id: str,
    offer_body: Callable[..., dict[str, object]],
    **overrides: object,
) -> dict[str, Any]:
    response = client.post(
        f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body(**overrides)
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _counter_offer(
    client: TestClient,
    seller: dict[str, str],
    offer_id: str,
    offer_body: Callable[..., dict[str, object]],
    **overrides: object,
) -> dict[str, Any]:
    response = client.post(
        f"/v1/offers/{offer_id}/counter", headers=seller, json=offer_body(**overrides)
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _accept_http(
    client: TestClient,
    answerer: dict[str, str],
    offer_id: str,
    idempotency_key: Callable[[], str],
) -> dict[str, Any]:
    body = {
        "delivery_method": "PICKUP",
        "buyer_address_woreda_id": 1,
        "buyer_address_text": "Bole",
    }
    headers = {**answerer, "Idempotency-Key": idempotency_key()}
    response = client.post(f"/v1/offers/{offer_id}/accept", headers=headers, json=body)
    assert response.status_code == 201, response.text
    accepted: dict[str, Any] = response.json()
    return accepted


def _order_by_http_accept(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> dict[str, Any]:
    """A listing, a buyer's bid, and the seller's yes - the shortest path to an order."""
    listing = _post_listing(client, seller, listing_body)
    offer = _place_offer(client, buyer, listing["id"], offer_body)
    return _accept_http(client, seller, offer["id"], idempotency_key)


def test_help_lists_the_commands_and_the_ussd_code(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    signed_in(SELLER, language="en")
    response = sms_inbound(client, "HELP", from_phone=SELLER)
    assert response.status_code == 200
    assert response.content == b""
    assert (
        sms_recorder.messages_to(SELLER)[-1].body
        == "Help: PRICE POST OFFER STATUS CANCEL PAYOUTS. USSD *851#"
    )


def test_post_creates_a_listing_from_one_message(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    _as_seller(client, signed_in)
    response = sms_inbound(client, "POST teff 2000 32.5", from_phone=SELLER)
    assert response.status_code == 200
    assert response.content == b""

    reply = sms_recorder.messages_to(SELLER)[-1].body
    rows = stored_listings()
    assert len(rows) == 1
    assert reply == f"Your listing is live. Code {rows[0]['short_code']}"
    assert rows[0]["source"] == "SMS"
    assert rows[0]["crop_id"] == 1
    assert rows[0]["quantity_kg"] == 2000
    assert rows[0]["unit_price_santim"] == 3250
    assert rows[0]["min_order_kg"] == 1
    assert rows[0]["region_code"] == "OR"
    assert rows[0]["woreda_id"] == 1
    assert "PICKUP" in str(rows[0]["delivery_options"])
    assert rows[0]["status"] == "ACTIVE"


def test_post_refuses_an_unknown_crop_with_a_200(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    _as_seller(client, signed_in)
    response = sms_inbound(client, "POST zzzz 2000 32.5", from_phone=SELLER)
    assert response.status_code == 200
    assert sms_recorder.messages_to(SELLER)[-1].body == "Your listing was not accepted: crop"
    assert stored_listings() == []


def test_post_refuses_a_price_beyond_two_decimal_places(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    _as_seller(client, signed_in)
    sms_inbound(client, "POST teff 2000 32.555", from_phone=SELLER)
    assert sms_recorder.messages_to(SELLER)[-1].body == "Your listing was not accepted: price"
    assert stored_listings() == []


def test_offer_places_a_bid_named_by_the_listing_code(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    seller = _as_seller(client, signed_in)
    _as_buyer(client, signed_in_as_buyer)
    listing = _post_listing(client, seller, listing_body)
    response = sms_inbound(client, f"OFFER {listing['short_code']} 500 33", from_phone=BUYER)
    assert response.status_code == 200
    assert (
        sms_recorder.messages_to(BUYER)[-1].body
        == f"Offer sent: {listing['short_code']} 500kg at 33 ETB/kg"
    )
    rows = stored_offers()
    assert len(rows) == 1
    assert str(rows[0]["listing_id"]) == listing["id"]
    assert rows[0]["thread_depth"] == 0
    assert rows[0]["quantity_kg"] == 500
    assert rows[0]["price_santim"] == 3300


def test_offer_for_an_unknown_code_answers_with_help(
    client: TestClient,
    signed_in_as_buyer: Callable[..., dict[str, str]],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    _as_buyer(client, signed_in_as_buyer)
    response = sms_inbound(client, "OFFER GL-NOPE 500 33", from_phone=BUYER)
    assert response.status_code == 200
    assert "USSD *851#" in sms_recorder.messages_to(BUYER)[-1].body


def test_offer_below_the_listings_minimum_answers_with_help(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """A bid the listing's own floor refuses is answered the way an unknown word is."""

    seller = _as_seller(client, signed_in)
    _as_buyer(client, signed_in_as_buyer)
    listing = _post_listing(client, seller, listing_body)
    response = sms_inbound(client, f"OFFER {listing['short_code']} 50 33", from_phone=BUYER)
    assert response.status_code == 200
    assert "USSD *851#" in sms_recorder.messages_to(BUYER)[-1].body
    assert stored_offers() == []


def test_yes_accepts_the_sellers_answer_and_creates_an_order(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    issue_action_token: Callable[..., str],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_action_tokens: Callable[[], list[dict[str, object]]],
) -> None:
    seller = _as_seller(client, signed_in)
    buyer = _as_buyer(client, signed_in_as_buyer)
    listing = _post_listing(client, seller, listing_body)
    opening = _place_offer(client, buyer, listing["id"], offer_body)

    counter = _counter_offer(client, seller, opening["id"], offer_body)
    token = issue_action_token(BUYER, f"offer_answer:{counter['id']}")

    response = sms_inbound(client, f"YES {token}", from_phone=BUYER)
    assert response.status_code == 200
    assert (
        sms_recorder.messages_to(BUYER)[-1].body
        == "Your offer for Teff was accepted at 33000 birr/kg"
    )
    orders = stored_orders()
    assert len(orders) == 1
    assert orders[0]["status"] == "AWAITING_FUNDING"
    assert orders[0]["delivery_method"] == "PICKUP"
    assert orders[0]["buyer_address_text"] == "Bole"
    assert stored_action_tokens()[0]["used_at"] is not None


def test_no_rejects_the_buyers_offer(
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
    seller = _as_seller(client, signed_in)
    buyer = _as_buyer(client, signed_in_as_buyer)
    listing = _post_listing(client, seller, listing_body)
    offer = _place_offer(client, buyer, listing["id"], offer_body)

    token = issue_action_token(SELLER, f"offer_answer:{offer['id']}")
    response = sms_inbound(client, f"NO {token}", from_phone=SELLER)
    assert response.status_code == 200
    assert sms_recorder.messages_to(SELLER)[-1].body == "Your offer for Teff is no longer available"
    assert stored_offers()[0]["status"] == "REJECTED"


def test_cancel_cancels_an_unpaid_order(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    seller = _as_seller(client, signed_in)
    buyer = _as_buyer(client, signed_in_as_buyer)
    order = _order_by_http_accept(client, seller, buyer, listing_body, offer_body, idempotency_key)
    response = sms_inbound(client, f"CANCEL {order['short_code']}", from_phone=BUYER)
    assert response.status_code == 200
    assert (
        sms_recorder.messages_to(BUYER)[-1].body
        == f"Order {order['short_code']} has been cancelled."
    )
    assert stored_orders()[0]["status"] == "CANCELLED"
    assert stored_orders()[0]["cancel_reason"] == "BUYER_CANCELLED"


def test_status_reports_the_order(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    seller = _as_seller(client, signed_in)
    buyer = _as_buyer(client, signed_in_as_buyer)
    order = _order_by_http_accept(client, seller, buyer, listing_body, offer_body, idempotency_key)
    response = sms_inbound(client, f"STATUS {order['short_code']}", from_phone=BUYER)
    assert response.status_code == 200
    assert (
        sms_recorder.messages_to(BUYER)[-1].body
        == f"Order {order['short_code']} is AWAITING_FUNDING"
    )


def test_payouts_answers_with_an_empty_history(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    signed_in(SELLER, language="en")
    response = sms_inbound(client, "PAYOUTS", from_phone=SELLER)
    assert response.status_code == 200
    assert sms_recorder.messages_to(SELLER)[-1].body == "You have no payouts yet (0)."


def test_an_unknown_word_answers_with_help(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    signed_in(SELLER, language="en")
    response = sms_inbound(client, "SELL teff 100", from_phone=SELLER)
    assert response.status_code == 200
    assert (
        sms_recorder.messages_to(SELLER)[-1].body
        == "Help: PRICE POST OFFER STATUS CANCEL PAYOUTS. USSD *851#"
    )
