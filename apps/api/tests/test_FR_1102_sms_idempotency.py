"""FR-1102, the SMS idempotency key: one intent, one effect, however often the gateway retries.

A gateway retries a webhook it did not see answered. The key is made from the sender and the raw
text - ``sms:{from}:{sha256(text)}`` - so two identical bodies from one handset are one intent,
while the same words from two handsets are two. These tests hold both halves: the retry writes
once and gets the first answer back, and a different body from the same sender is a second
intent.

The stored rows are read from PostgreSQL, not from the API, because the question is how many
rows exist and only the database can answer that without being believed on its own.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.providers.sms import RecordingSmsProvider

pytestmark = pytest.mark.integration

SELLER = "+251911000001"
OTHER_SELLER = "+251911000009"
BUYER = "+251911000003"


def _profile(client: TestClient, headers: dict[str, str]) -> None:
    updated = client.patch(
        "/v1/me",
        headers=headers,
        json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
    )
    assert updated.status_code == 200, updated.text


def test_an_identical_post_twice_writes_one_listing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    _profile(client, signed_in(SELLER, language="en"))

    first = sms_inbound(client, "POST teff 2000 32.5", from_phone=SELLER)
    second = sms_inbound(client, "POST teff 2000 32.5", from_phone=SELLER)
    assert first.status_code == second.status_code == 200

    rows = stored_listings()
    assert len(rows) == 1
    replies = [message.body for message in sms_recorder.messages_to(SELLER)]
    # The first message to this handset was the sign-in code; the last two are the replies.
    assert replies[-2] == replies[-1]
    assert str(rows[0]["short_code"]) in replies[-1]


def test_a_different_body_from_the_same_sender_writes_another_listing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    sms_inbound: Callable[..., httpx.Response],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    _profile(client, signed_in(SELLER, language="en"))

    sms_inbound(client, "POST teff 2000 32.5", from_phone=SELLER)
    sms_inbound(client, "POST teff 3000 32.5", from_phone=SELLER)
    rows = stored_listings()
    assert len(rows) == 2
    assert {row["quantity_kg"] for row in rows} == {2000, 3000}


def test_the_same_words_from_two_hands_are_two_intents(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    sms_inbound: Callable[..., httpx.Response],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    """The sender is part of the key: two farmers may post the same sentence."""

    _profile(client, signed_in(SELLER, language="en"))
    _profile(client, signed_in(OTHER_SELLER, language="en"))

    sms_inbound(client, "POST teff 2000 32.5", from_phone=SELLER)
    sms_inbound(client, "POST teff 2000 32.5", from_phone=OTHER_SELLER)
    rows = stored_listings()
    assert len(rows) == 2
    assert len({row["short_code"] for row in rows}) == 2


def test_an_identical_offer_twice_writes_one_offer(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    sms_inbound: Callable[..., httpx.Response],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    seller = signed_in(SELLER, language="en")
    signed_in_as_buyer(BUYER, language="en")
    created = client.post("/v1/listings", headers=seller, json=listing_body())
    assert created.status_code == 201, created.text
    code = created.json()["short_code"]

    text = f"OFFER {code} 500 33"
    sms_inbound(client, text, from_phone=BUYER)
    sms_inbound(client, text, from_phone=BUYER)
    assert len(stored_offers()) == 1
