"""The provider seam: the recording backend still records, and the gateway adapter
refuses to pretend a message was sent before its credentials resolve.
"""

from __future__ import annotations

import pytest

from apps.api.providers.sms import AfricasTalkingProvider, RecordingSmsProvider, SmsMessage
from packages.shared.messaging.provider import SendResult
from packages.shared.messaging.queues import SmsPriority

pytestmark = pytest.mark.unit


async def test_the_recording_backend_still_records_messages() -> None:
    """RecordingSmsProvider is untouched: it keeps messages and accepts them."""
    provider = RecordingSmsProvider()
    receipt = await provider.send(SmsMessage(to="+251911000001", body="hello"))
    assert receipt.accepted is True
    assert provider.messages_to("+251911000001")[0].body == "hello"


async def test_africas_talking_refuses_to_send_until_its_credentials_resolve() -> None:
    """The gateway adapter raises rather than silently dropping a message."""
    provider = AfricasTalkingProvider()
    with pytest.raises(NotImplementedError) as failure:
        await provider.send(
            "+251911000001",
            "hello",
            priority=SmsPriority.Q1,
            template_id="auth_otp",
        )
    assert "credentials resolve" in str(failure.value)


def test_send_result_carries_the_provider_s_verdict() -> None:
    accepted = SendResult(accepted=True, provider_ref="ref-1")
    assert (accepted.accepted, accepted.provider_ref, accepted.reason) == (True, "ref-1", None)
    refused = SendResult(accepted=False, reason="insufficient credit")
    assert refused.accepted is False
    assert refused.provider_ref is None
    assert refused.reason == "insufficient credit"
