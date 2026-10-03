"""FR-102: the rules that make a verification code safe to send over SMS.

Six digits, gone after five minutes, five wrong guesses, a minute between codes, and five a
day per number. Each of those is a separate guard against a different abuse, and each is
checked here against a real Redis rather than a stand-in, because the guards are made of
counters and expiry and that is exactly what a fake store gets wrong.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_FR_102_the_code_is_six_digits_and_lives_for_five_minutes(
    client: TestClient, code_for: Callable[[str], str]
) -> None:
    """The trader is told how long the code is good for, and it is really six digits."""
    phone_e164 = "+251933333333"

    response = client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    assert response.status_code == 200
    assert response.json() == {"expires_in_seconds": 300, "resend_after_seconds": 60}
    code = code_for(phone_e164)
    assert len(code) == 6
    assert code.isdigit()


def test_FR_102_a_wrong_code_is_refused_and_costs_a_guess(client: TestClient) -> None:
    """A wrong guess says how many guesses are left, so the trader can stop guessing."""
    phone_e164 = "+251933333334"
    client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    response = client.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": phone_e164,
            "code": "000000",
            "terms_version": "2026-01-01",
        },
    )

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "OTP_EXPIRED"
    assert error["details"]["reason"] == "incorrect_code"
    assert error["details"]["attempts_remaining"] == 4


def test_FR_102_the_right_code_is_accepted_once_and_only_once(
    client: TestClient, code_for: Callable[[str], str]
) -> None:
    """A code is spent the moment it works, so it cannot be used a second time."""
    phone_e164 = "+251933333335"
    client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})
    body = {
        "phone_e164": phone_e164,
        "code": code_for(phone_e164),
        "terms_version": "2026-01-01",
    }

    first = client.post("/v1/auth/otp/verify", json=body)
    second = client.post("/v1/auth/otp/verify", json=body)

    assert first.status_code == 200
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "OTP_EXPIRED"
    assert second.json()["error"]["details"]["reason"] == "no_code_pending"


def test_FR_102_a_code_that_has_expired_is_refused(
    impatient_client: TestClient, code_for: Callable[[str], str]
) -> None:
    """After its lifetime the code is gone, exactly as if it had never been sent."""
    phone_e164 = "+251933333336"
    impatient_client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})
    code = code_for(phone_e164)
    time.sleep(2.5)

    response = impatient_client.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": phone_e164,
            "code": code,
            "terms_version": "2026-01-01",
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["details"]["reason"] == "no_code_pending"


def test_FR_102_a_second_code_within_the_cooldown_is_refused(client: TestClient) -> None:
    """Asking twice in a minute is a mis-tap, not two codes."""
    phone_e164 = "+251933333337"
    first = client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    second = client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    assert first.status_code == 200
    assert second.status_code == 429
    error = second.json()["error"]
    assert error["code"] == "RATE_LIMITED"
    assert error["details"]["scope"] == "resend_cooldown"
    assert 0 < error["details"]["retry_after_seconds"] <= 60


def test_FR_102_a_resend_after_the_cooldown_works(
    impatient_client: TestClient, code_for: Callable[[str], str]
) -> None:
    """Once the wait is over the trader gets a fresh code that actually works."""
    phone_e164 = "+251933333338"
    impatient_client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})
    time.sleep(1.2)

    resent = impatient_client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    assert resent.status_code == 200
    verified = impatient_client.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": phone_e164,
            "code": code_for(phone_e164),
            "terms_version": "2026-01-01",
        },
    )
    assert verified.status_code == 200


def test_FR_102_one_number_receives_at_most_five_codes_a_day(
    impatient_client: TestClient,
) -> None:
    """The daily ceiling holds even for somebody who waits out every cooldown."""
    phone_e164 = "+251933333339"

    for _ in range(5):
        sent = impatient_client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})
        assert sent.status_code == 200
        time.sleep(1.2)

    refused = impatient_client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    assert refused.status_code == 429
    error = refused.json()["error"]
    assert error["code"] == "RATE_LIMITED"
    assert error["details"]["scope"] == "codes_per_day"
    assert error["details"]["limit"] == 5


def test_FR_102_one_address_cannot_spray_codes_at_many_numbers(spray_client: TestClient) -> None:
    """The per-address ceiling counts across numbers, which the per-number one cannot see."""
    for index in range(20):
        sent = spray_client.post(
            "/v1/auth/otp/request",
            json={"phone_e164": f"+2519{index + 1:08d}"},
            headers={"x-forwarded-for": "10.0.0.9"},
        )
        assert sent.status_code == 200, sent.text

    refused = spray_client.post(
        "/v1/auth/otp/request",
        json={"phone_e164": "+251999999999"},
        headers={"x-forwarded-for": "10.0.0.9"},
    )

    assert refused.status_code == 429
    assert refused.json()["error"]["details"]["limit"] == 20


def test_FR_102_one_address_cannot_hammer_the_sign_in_endpoints(client: TestClient) -> None:
    """The per-minute ceiling stops a single caller from working through the endpoint."""
    statuses = [
        client.post(
            "/v1/auth/otp/request",
            json={"phone_e164": f"+2519{index + 1:08d}"},
            headers={"x-forwarded-for": "10.0.0.10"},
        ).status_code
        for index in range(12)
    ]

    assert statuses[:10] == [200] * 10
    assert statuses[10:] == [429, 429]
