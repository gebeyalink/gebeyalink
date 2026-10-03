"""AC-1.2: five wrong guesses and the code is finished.

The lockout is the whole reason a six-digit code is safe to send over SMS. What matters is
not only that the sixth attempt is refused, but that the code behind it is destroyed: a
locked code that stayed in the store would leave an attacker one more guess than the limit
allows.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from redis import Redis as SyncRedis

pytestmark = pytest.mark.integration


def test_AC_1_2_the_sixth_attempt_is_locked_out_and_the_code_is_destroyed(
    client: TestClient, redis_url: str, code_for: Callable[[str], str]
) -> None:
    """Five wrong guesses, then the sixth attempt is refused and the code is gone."""
    phone_e164 = "+251944400010"
    client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})
    real_code = code_for(phone_e164)

    for attempt in range(1, 6):
        response = client.post(
            "/v1/auth/otp/verify",
            json={
                "phone_e164": phone_e164,
                "code": "000000" if real_code != "000000" else "111111",
                "terms_version": "2026-01-01",
            },
        )
        assert response.status_code == 401, f"attempt {attempt} should not have locked yet"
        assert response.json()["error"]["details"]["attempts_remaining"] == 5 - attempt

    sixth = client.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": phone_e164,
            "code": "000000" if real_code != "000000" else "111111",
            "terms_version": "2026-01-01",
        },
    )

    assert sixth.status_code == 429
    assert sixth.json()["error"]["code"] == "OTP_LOCKED"

    store = SyncRedis.from_url(redis_url)
    stored = store.hgetall(f"verification-code:{phone_e164}")
    store.close()
    assert stored == {}, "the code must not survive its own lockout"


def test_AC_1_2_the_correct_code_is_refused_once_the_guesses_are_spent(
    client: TestClient, code_for: Callable[[str], str]
) -> None:
    """The sixth attempt is past the limit, so it is refused whatever it contains.

    Checking the guess before the budget would make the limit a suggestion: the one attempt
    an attacker really wants is the last one.
    """
    phone_e164 = "+251944400011"
    client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})
    real_code = code_for(phone_e164)
    wrong_code = "000000" if real_code != "000000" else "111111"

    for _ in range(5):
        client.post(
            "/v1/auth/otp/verify",
            json={
                "phone_e164": phone_e164,
                "code": wrong_code,
                "terms_version": "2026-01-01",
            },
        )

    with_the_right_code = client.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": phone_e164,
            "code": real_code,
            "terms_version": "2026-01-01",
        },
    )

    assert with_the_right_code.status_code == 429
    assert with_the_right_code.json()["error"]["code"] == "OTP_LOCKED"


def test_AC_1_2_a_locked_number_stays_locked_until_a_new_code_is_requested(
    client: TestClient, code_for: Callable[[str], str]
) -> None:
    """Asking again is the way out, which is also why a new code clears the lock."""
    phone_e164 = "+251944400012"
    client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})
    real_code = code_for(phone_e164)
    wrong_code = "000000" if real_code != "000000" else "111111"

    for _ in range(6):
        client.post(
            "/v1/auth/otp/verify",
            json={
                "phone_e164": phone_e164,
                "code": wrong_code,
                "terms_version": "2026-01-01",
            },
        )

    still_locked = client.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": phone_e164,
            "code": real_code,
            "terms_version": "2026-01-01",
        },
    )
    assert still_locked.status_code == 429
    assert still_locked.json()["error"]["details"]["reason"] == "guesses_spent"
