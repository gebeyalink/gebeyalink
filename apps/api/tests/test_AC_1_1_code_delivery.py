"""AC-1.1: a trader who asks for a code gets one, in time, and it works.

The criterion joins two things a trader would call one event: the message arriving, and the
code in it being usable for the advertised five minutes. Both are checked against the
recorded message and a real Redis, so a code that was generated but never delivered — or
delivered with an expiry nobody told the trader about — fails here.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from redis import Redis as SyncRedis

from apps.api.providers.sms import RecordingSmsProvider

pytestmark = pytest.mark.integration


def test_AC_1_1_a_code_reaches_the_trader_and_signs_them_in(
    client: TestClient, code_for: Callable[[str], str]
) -> None:
    """End to end on the platform's own terms: asked, answered, signed in."""
    phone_e164 = "+251944400001"
    before = time.monotonic()

    requested = client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})
    elapsed = time.monotonic() - before

    assert requested.status_code == 200
    assert elapsed < 30, f"the code took {elapsed:.1f}s to reach the trader"

    verified = client.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": phone_e164,
            "code": code_for(phone_e164),
            "terms_version": "2026-01-01",
        },
    )

    assert verified.status_code == 200
    assert verified.json()["user"]["phone_e164"] == phone_e164


def test_AC_1_1_the_stored_code_is_given_the_advertised_lifetime(
    client: TestClient, redis_url: str, code_for: Callable[[str], str]
) -> None:
    """The five minutes the trader was told about are the five minutes the code gets."""
    phone_e164 = "+251944400002"
    response = client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})
    advertised = response.json()["expires_in_seconds"]

    store = SyncRedis.from_url(redis_url)
    seconds_left = store.ttl(f"verification-code:{phone_e164}")
    store.close()

    assert advertised == 300
    assert seconds_left > 0
    assert seconds_left <= advertised


def test_AC_1_1_the_message_names_the_number_it_was_sent_to(
    client: TestClient, sms_recorder: RecordingSmsProvider
) -> None:
    """One message, to the number that asked, and no other."""
    phone_e164 = "+251944400003"

    client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    assert len(sms_recorder.messages_to(phone_e164)) == 1
    assert len(sms_recorder.sent) == 1
