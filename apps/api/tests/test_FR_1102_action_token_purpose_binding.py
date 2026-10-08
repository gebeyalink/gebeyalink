"""FR-1102, the purpose prefix: a token names one action, on one row.

A token is a string with a prefix and a target - ``offer_answer:<offer>`` or
``delivery_confirm:<order>`` - and the prefix decides which action YES or NO performs. That is
what stops a token minted for a delivery from being spent on a bid, and what keeps a token for
one offer from answering another. These tests hold both the prefix and the target: the action is
the one the prefix names, and the row is the one the target carries.
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


def _listing_with_offer(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> dict[str, Any]:
    created = client.post("/v1/listings", headers=seller, json=listing_body())
    assert created.status_code == 201, created.text
    listing = created.json()
    offer = client.post(f"/v1/listings/{listing['id']}/offers", headers=buyer, json=offer_body())
    assert offer.status_code == 201, offer.text
    bid: dict[str, Any] = offer.json()
    return bid


def _status_of(rows: list[dict[str, object]], offer_id: str) -> object:
    return next(row["status"] for row in rows if str(row["id"]) == offer_id)


def test_the_target_in_the_purpose_is_the_offer_that_is_rejected(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    issue_action_token: Callable[..., str],
    sms_inbound: Callable[..., httpx.Response],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """Two open bids, one token: the token answers the bid it names and no other."""

    seller = signed_in(SELLER, language="en")
    buyer = signed_in_as_buyer(BUYER, language="en")
    first = _listing_with_offer(client, seller, buyer, listing_body, offer_body)
    second = _listing_with_offer(client, seller, buyer, listing_body, offer_body)
    token = issue_action_token(SELLER, f"offer_answer:{first['id']}")

    sms_inbound(client, f"NO {token}", from_phone=SELLER)

    rows = stored_offers()
    assert _status_of(rows, first["id"]) == "REJECTED"
    assert _status_of(rows, second["id"]) == "OPEN"


def test_a_delivery_token_cannot_be_spent_on_an_offer(
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
    """The prefix is the action: ``delivery_confirm`` is refused by NO, which answers offers."""

    seller = signed_in(SELLER, language="en")
    buyer = signed_in_as_buyer(BUYER, language="en")
    offer = _listing_with_offer(client, seller, buyer, listing_body, offer_body)
    token = issue_action_token(BUYER, f"delivery_confirm:{offer['id']}")

    sms_inbound(client, f"NO {token}", from_phone=SELLER)
    assert sms_recorder.messages_to(SELLER)[-1].body == f"That action code did not work: {token}"
    assert stored_offers()[0]["status"] == "OPEN"


def test_an_offer_token_whose_offer_does_not_exist_is_refused(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    issue_action_token: Callable[..., str],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    signed_in(SELLER, language="en")
    token = issue_action_token(SELLER, "offer_answer:00000000-0000-0000-0000-000000000000")
    sms_inbound(client, f"NO {token}", from_phone=SELLER)
    assert sms_recorder.messages_to(SELLER)[-1].body == f"That action code did not work: {token}"
