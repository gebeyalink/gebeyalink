"""FR-101: an Ethiopian mobile number in E.164 form.

A trader signs up with a number and nothing else, so this is the first thing the platform
checks and the thing most worth getting right: a number that is refused here is refused in
the same shape as every other failure, and a number that is accepted can actually receive a
message.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from apps.api.providers.sms import RecordingSmsProvider

pytestmark = pytest.mark.integration

ACCEPTED_NUMBERS = ["+251912345678", "+251712345678"]
REJECTED_NUMBERS = [
    "+25191234567",  # one digit short
    "+2519123456789",  # one digit long
    "+251612345678",  # not a mobile prefix
    "+25191234567a",  # not all digits
    "251912345678",  # no country code
    "+251912345678 ",  # trailing space
    "+1 202 555 0143",  # a plausible number from another country
    "",
]


@pytest.mark.parametrize("phone_e164", ACCEPTED_NUMBERS)
def test_FR_101_ethiopian_mobile_number_is_accepted(
    client: TestClient,
    sms_recorder: RecordingSmsProvider,
    phone_e164: str,
) -> None:
    """Both Ethiopian mobile prefixes reach the trader as a text message."""
    response = client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    assert response.status_code == 200
    assert [message.to for message in sms_recorder.messages_to(phone_e164)] == [phone_e164]


@pytest.mark.parametrize("phone_e164", REJECTED_NUMBERS)
def test_FR_101_anything_else_is_refused_with_validation_error(
    client: TestClient, phone_e164: str
) -> None:
    """Every other shape is turned away in the shared failure envelope."""
    response = client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["trace_id"]
    assert "phone_e164" in error["details"]["fields"]


def test_FR_101_failure_carries_the_language_the_caller_asked_for(client: TestClient) -> None:
    """The sentence is written in the caller's language, the code is not negotiable."""
    response = client.post(
        "/v1/auth/otp/request",
        json={"phone_e164": "+25191234567"},
        headers={"accept-language": "en"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "The submitted details are not valid."


def test_FR_101_every_failure_shares_one_shape(client: TestClient) -> None:
    """A client parses one shape, whatever went wrong and whatever it sent."""
    refusals = [
        client.post("/v1/auth/otp/request", json={"phone_e164": "+25191234567"}),
        client.post("/v1/auth/otp/verify", json={"phone_e164": "+251912345678", "code": "12345"}),
        client.post("/v1/auth/refresh"),
        client.post("/v1/auth/does-not-exist"),
    ]

    for response in refusals:
        error = response.json()["error"]
        assert set(error) == {"code", "message", "details", "trace_id"}
        assert response.headers["X-Trace-Id"] == error["trace_id"]


def test_FR_101_a_number_missing_entirely_is_refused(client: TestClient) -> None:
    """An empty body is refused the same way a wrong number is, not with a stack trace."""
    response = client.post("/v1/auth/otp/request", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_FR_101_a_successful_request_does_not_say_whether_the_account_exists(
    client: TestClient, code_for: Callable[[str], str]
) -> None:
    """Asking twice looks identical whether or not the number is already registered.

    Answering differently would turn this endpoint into a way to find out who trades here.
    """
    phone_e164 = "+251911111111"

    first = client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})
    client.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": phone_e164,
            "code": code_for(phone_e164),
            "terms_version": "2026-01-01",
        },
    )
    second = client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


def test_FR_101_request_headers_are_not_required(client: TestClient) -> None:
    """The endpoint works without an Accept-Language or any other optional header."""
    response = client.post("/v1/auth/otp/request", json={"phone_e164": "+251922222222"})

    assert response.status_code == 200
