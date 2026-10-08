"""FR-1102, a number the platform does not know: point at the door, do not act.

A short code is advertised widely and anybody may text it, including someone who has never
registered. That is not an error and it is not a command: the reply is the registration
prompt in the default language, and the database is not touched. These tests hold the second
half of that as firmly as the first - an unregistered sender's POST does not create a
listing, and their token does not spend.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.providers.sms import RecordingSmsProvider

pytestmark = pytest.mark.integration

STRANGER = "+251900000042"


def test_a_stranger_is_told_how_to_register(
    client: TestClient,
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    response = sms_inbound(client, "HELP", from_phone=STRANGER)
    assert response.status_code == 200
    messages = sms_recorder.messages_to(STRANGER)
    assert len(messages) == 1
    # No account means no stored language, so the reply is the default one.
    assert "USSD *851#" in messages[0].body


def test_a_stranger_is_answered_the_same_way_for_every_verb(
    client: TestClient,
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    """Registration comes before any command, including the ones that write."""

    for text in ("POST teff 2000 32.5", "PRICE teff", "PAYOUTS", "STATUS GL-000000"):
        response = sms_inbound(client, text, from_phone=STRANGER)
        assert response.status_code == 200
        body = sms_recorder.messages_to(STRANGER)[-1].body
        assert "USSD *851#" in body, text


def test_a_stranger_posting_a_listing_creates_nothing(
    client: TestClient,
    sms_inbound: Callable[..., httpx.Response],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    sms_inbound(client, "POST teff 2000 32.5", from_phone=STRANGER)
    assert stored_listings() == []
