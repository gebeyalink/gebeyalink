"""FR-105: which language a trader reads, and where that choice lives.

Three languages ship at the pilot's start and Amharic is the one assumed when nobody says
otherwise. The choice belongs to the trader, so it is stored on the account; a request
header only says how one reply should be worded and is never allowed to overwrite it.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

LANGUAGES = ["am", "om", "en"]


@pytest.mark.parametrize(
    ("language", "phone_e164"),
    [
        ("am", "+251911100001"),
        ("om", "+251911100002"),
        ("en", "+251911100003"),
    ],
)
def test_FR_105_a_trader_can_state_their_language_at_registration(
    client: TestClient,
    accounts: Callable[[], list[dict[str, object]]],
    sign_in: Callable[..., httpx.Response],
    language: str,
    phone_e164: str,
) -> None:
    """Each of the three launch languages is accepted and written down."""
    response = sign_in(phone_e164, language=language)

    assert response.status_code == 200
    assert response.json()["user"]["language"] == language
    assert accounts()[0]["language"] == language


def test_FR_105_amharic_is_assumed_when_nobody_says_otherwise(
    client: TestClient,
    accounts: Callable[[], list[dict[str, object]]],
    sign_in: Callable[..., httpx.Response],
) -> None:
    """A client that states no preference at all still gets a working account."""
    response = sign_in("+251911100010")

    assert response.status_code == 200
    assert response.json()["user"]["language"] == "am"
    assert accounts()[0]["language"] == "am"


def test_FR_105_a_stored_preference_survives_a_later_sign_in(
    client: TestClient,
    accounts: Callable[[], list[dict[str, object]]],
    code_for: Callable[[str], str],
    sign_in: Callable[..., httpx.Response],
) -> None:
    """Reading one reply in another language must not rewrite the trader's own choice.

    The trader chose Oromo when they registered. A client that happens to send an English
    Accept-Language header is asking about this reply, not changing a setting.
    """
    phone_e164 = "+251911100020"
    sign_in(phone_e164, language="om")

    client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})
    again = client.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": phone_e164,
            "code": code_for(phone_e164),
            "terms_version": "2026-01-01",
        },
        headers={"accept-language": "en"},
    )

    assert again.status_code == 200
    assert again.json()["user"]["language"] == "om"
    assert accounts()[0]["language"] == "om"


def test_FR_105_a_trader_can_change_their_own_language(
    client: TestClient,
    accounts: Callable[[], list[dict[str, object]]],
    code_for: Callable[[str], str],
    sign_in: Callable[..., httpx.Response],
) -> None:
    """Stating a language later is a change of preference, and it is honoured."""
    phone_e164 = "+251911100021"
    sign_in(phone_e164, language="om")

    client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})
    changed = client.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": phone_e164,
            "code": code_for(phone_e164),
            "terms_version": "2026-01-01",
            "language": "en",
        },
    )

    assert changed.status_code == 200
    assert changed.json()["user"]["language"] == "en"
    assert accounts()[0]["language"] == "en"


def test_FR_105_the_three_languages_explain_a_failure_three_different_ways(
    client: TestClient,
) -> None:
    """Distinct sentences, not one English string under three different headers."""
    messages = set()
    for index, language in enumerate(LANGUAGES):
        # A number nobody has a code for: the same real failure, asked for three ways.
        response = client.post(
            "/v1/auth/otp/verify",
            json={
                "phone_e164": f"+25191110003{index}",
                "code": "111111",
                "terms_version": "2026-01-01",
            },
            headers={"accept-language": language},
        )
        assert response.status_code == 401
        messages.add(response.json()["error"]["message"])

    assert len(messages) == 3, f"expected one sentence per language, got {messages}"


@pytest.mark.parametrize(
    ("header", "phone_e164"),
    [
        ("fr", "+251911100061"),
        ("am-ET", "+251911100062"),
        ("english", "+251911100063"),
        ("zz", "+251911100064"),
        ("de,en;q=0.9", "+251911100065"),
    ],
)
def test_FR_105_an_unknown_reading_language_falls_back_rather_than_failing(
    client: TestClient,
    code_for: Callable[[str], str],
    header: str,
    phone_e164: str,
) -> None:
    """Asking to read in a language we do not carry is not an error; it gets Amharic."""
    client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    response = client.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": phone_e164,
            "code": code_for(phone_e164),
            "terms_version": "2026-01-01",
        },
        headers={"accept-language": header},
    )

    assert response.status_code == 200
    assert response.json()["user"]["language"] == "am"


def test_FR_105_a_language_the_platform_does_not_carry_is_refused_when_stated(
    client: TestClient, code_for: Callable[[str], str]
) -> None:
    """Stating a preference is a claim about stored data, so it has to name a real one."""
    phone_e164 = "+251911100050"
    client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    response = client.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": phone_e164,
            "code": code_for(phone_e164),
            "terms_version": "2026-01-01",
            "language": "fr",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
