"""The provider seam: the recording backend still records, and the gateway adapter
refuses to pretend a message was sent before its credentials resolve.
"""

from __future__ import annotations

import pytest

from apps.api.core.config import Environment
from apps.api.providers.sms import (
    AfricasTalkingProvider,
    ConfigurationError,
    RecordingMessagingProvider,
    RecordingSmsProvider,
    SmsMessage,
    build_messaging_provider,
    log_recording_provider_startup,
    mask_phone,
)
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


async def test_the_recording_messaging_provider_records_into_the_store_it_is_given() -> None:
    """The drain's backend is the sign-in flow's recorder: one store, one record."""
    recorder = RecordingSmsProvider()
    provider = build_messaging_provider("recording", recorder=recorder)
    assert isinstance(provider, RecordingMessagingProvider)

    result = await provider.send(
        "+251911000021", "hello", priority=SmsPriority.Q2, template_id="order_shipped"
    )

    assert result.accepted is True
    assert result.provider_ref is not None
    assert recorder.messages_to("+251911000021")[0].body == "hello"


async def test_a_recording_messaging_provider_without_a_store_keeps_its_own() -> None:
    """A worker has no code to read, so a recorder built for it is the process's own."""
    provider = build_messaging_provider("recording")
    assert isinstance(provider, RecordingMessagingProvider)

    await provider.send("+251911000021", "hello", priority=SmsPriority.Q1, template_id="auth_otp")

    assert provider.recorder.messages_to("+251911000021")[0].body == "hello"


def test_an_unknown_messaging_backend_is_refused() -> None:
    """A name this process cannot build is refused rather than silently recorded."""
    with pytest.raises(ConfigurationError):
        build_messaging_provider("africas_talking")


def test_the_phone_mask_keeps_the_country_code_and_the_last_two_digits() -> None:
    assert mask_phone("+251911000021") == "+2519***21"
    assert mask_phone("+251") == "***"


def test_the_recording_provider_is_a_warning_away_from_production_and_an_error_in_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The fallback is quiet to nobody: loud in production, and still audible before it."""
    caplog.set_level("INFO", logger="gebeyalink.sms")

    log_recording_provider_startup("recording", Environment.DEVELOPMENT)
    log_recording_provider_startup("recording", Environment.PRODUCTION)

    levels = {record.levelname for record in caplog.records if record.name == "gebeyalink.sms"}
    assert levels == {"WARNING", "CRITICAL"}


def test_a_live_provider_is_not_nagged_about(caplog: pytest.LogCaptureFixture) -> None:
    """The warning is about the fallback, so a real backend says nothing."""
    caplog.set_level("INFO", logger="gebeyalink.sms")

    log_recording_provider_startup("africas_talking", Environment.PRODUCTION)

    assert caplog.records == []
