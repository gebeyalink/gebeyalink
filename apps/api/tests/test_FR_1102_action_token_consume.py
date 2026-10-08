"""FR-1102, the action token as a spend: once, before its deadline, or not at all.

The token is what stands between a swapped SIM and a contested action, so the interesting cases
are the ones where it does *not* work: a token already spent, a token whose day has passed, and
a string that was never a token at all. Each produces the same sentence to the trader, and only
the log is told which it was - the distinction is forensics, not a message a buyer can act on.

The row is written straight into the table the way issuance will write it, because issuing is a
later slice; everything downstream of the row is real.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.providers.sms import RecordingSmsProvider

pytestmark = pytest.mark.integration

SELLER = "+251911000001"
BUYER = "+251911000003"


def _listing_and_counter(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> dict[str, Any]:
    """A listing, a buyer's bid and the seller's counter: a token waiting to be spent."""
    seller = signed_in(SELLER, language="en")
    buyer = signed_in_as_buyer(BUYER, language="en")
    updated = client.patch(
        "/v1/me",
        headers=buyer,
        json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
    )
    assert updated.status_code == 200, updated.text
    created = client.post("/v1/listings", headers=seller, json=listing_body())
    assert created.status_code == 201, created.text
    listing = created.json()
    offer = client.post(f"/v1/listings/{listing['id']}/offers", headers=buyer, json=offer_body())
    assert offer.status_code == 201, offer.text
    counter = client.post(
        f"/v1/offers/{offer.json()['id']}/counter", headers=seller, json=offer_body()
    )
    assert counter.status_code == 200, counter.text
    answered: dict[str, Any] = counter.json()
    return answered


def test_a_token_is_spent_by_the_first_yes(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    issue_action_token: Callable[..., str],
    sms_inbound: Callable[..., httpx.Response],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_action_tokens: Callable[[], list[dict[str, object]]],
) -> None:
    counter = _listing_and_counter(client, signed_in, signed_in_as_buyer, listing_body, offer_body)
    token = issue_action_token(BUYER, f"offer_answer:{counter['id']}")

    sms_inbound(client, f"YES {token}", from_phone=BUYER)
    assert len(stored_orders()) == 1
    assert stored_action_tokens()[0]["used_at"] is not None


def test_a_spent_token_cannot_be_spent_again(
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
    counter = _listing_and_counter(client, signed_in, signed_in_as_buyer, listing_body, offer_body)
    token = issue_action_token(BUYER, f"offer_answer:{counter['id']}")

    sms_inbound(client, f"YES {token}", from_phone=BUYER)
    sms_inbound(client, f"YES {token}", from_phone=BUYER)

    assert sms_recorder.messages_to(BUYER)[-1].body == f"That action code did not work: {token}"
    assert len(stored_orders()) == 1


def test_an_expired_token_is_refused(
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
    counter = _listing_and_counter(client, signed_in, signed_in_as_buyer, listing_body, offer_body)
    token = issue_action_token(BUYER, f"offer_answer:{counter['id']}", ttl=timedelta(hours=-1))

    sms_inbound(client, f"YES {token}", from_phone=BUYER)
    assert sms_recorder.messages_to(BUYER)[-1].body == f"That action code did not work: {token}"
    assert stored_orders() == []


def test_a_token_marked_used_at_issue_is_refused(
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
    counter = _listing_and_counter(client, signed_in, signed_in_as_buyer, listing_body, offer_body)
    token = issue_action_token(BUYER, f"offer_answer:{counter['id']}", used_at=datetime.now(UTC))

    sms_inbound(client, f"YES {token}", from_phone=BUYER)
    assert sms_recorder.messages_to(BUYER)[-1].body == f"That action code did not work: {token}"
    assert stored_orders() == []


def test_a_string_that_was_never_a_token_is_refused(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    signed_in(BUYER, language="en")
    sms_inbound(client, "YES NOSUCHTOKEN", from_phone=BUYER)
    assert sms_recorder.messages_to(BUYER)[-1].body == "That action code did not work: NOSUCHTOKEN"
