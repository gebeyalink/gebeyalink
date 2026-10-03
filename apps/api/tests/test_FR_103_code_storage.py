"""FR-103: a verification code is never stored in readable form.

The store holds a salt and a digest and nothing else, so a copy of Redis is not a list of
codes waiting to be replayed against somebody's account. The message the trader receives is
the only place the digits exist.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis import Redis as SyncRedis

from apps.api.core.hashing import digest, generate_salt
from apps.api.providers.sms import RecordingSmsProvider, SmsMessage, SmsReceipt

pytestmark = pytest.mark.integration


def test_FR_103_the_readable_code_is_never_written_to_the_store(
    client: TestClient,
    clean_store: None,
    redis_url: str,
    code_for: Callable[[str], str],
) -> None:
    """Nothing in Redis contains the digits the trader was sent."""
    phone_e164 = "+251944444444"
    client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})
    code = code_for(phone_e164)

    store = SyncRedis.from_url(redis_url)
    serialised = [
        dumped
        for key in store.scan_iter(match="verification-*")
        if (dumped := store.dump(key)) is not None
    ]
    everything = b"".join(serialised)
    store.close()

    assert code.encode() not in everything
    assert b"digest" in everything
    assert b"salt" in everything


def test_FR_103_the_stored_digest_is_salted_so_two_codes_never_look_alike() -> None:
    """The same six digits hash differently twice, which defeats a precomputed table."""
    salt_one, salt_two = generate_salt(), generate_salt()

    assert digest("123456", salt_one) != digest("123456", salt_two)
    assert digest("123456", salt_one) == digest("123456", salt_one)


def test_FR_103_the_message_carries_the_code_and_warns_against_sharing_it(
    client: TestClient, sms_recorder: RecordingSmsProvider, code_for: Callable[[str], str]
) -> None:
    """The digits reach the handset, and the message says not to pass them on."""
    phone_e164 = "+251944444445"
    client.post(
        "/v1/auth/otp/request",
        json={"phone_e164": phone_e164},
        headers={"accept-language": "en"},
    )

    message = sms_recorder.messages_to(phone_e164)[-1]

    assert code_for(phone_e164) in message.body
    assert "Never share this code" in message.body


class RefusingSmsProvider:
    """A backend that turns every message away, the way a gateway refuses a number.

    Written here rather than in the application because no deployment wants this: its only
    job is to let a test stand in for a gateway that says no.
    """

    name = "refusing"

    async def send(self, message: SmsMessage) -> SmsReceipt:
        return SmsReceipt(provider=self.name, accepted=False, reference="refused")


def test_FR_103_a_rejected_message_leaves_no_code_behind(
    client: TestClient, redis_url: str
) -> None:
    """A message the backend refused must not cost the trader one of their guesses.

    Without this the trader holds a code they never received, and their first real attempt
    is spent discovering that.
    """
    phone_e164 = "+251944444446"
    application = cast(FastAPI, client.app)
    application.state.sms_provider = RefusingSmsProvider()

    response = client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    store = SyncRedis.from_url(redis_url)
    stored = store.hgetall(f"verification-code:{phone_e164}")
    store.close()

    assert response.status_code == 422
    assert response.json()["error"]["details"] == {"delivery": "rejected"}
    assert stored == {}
