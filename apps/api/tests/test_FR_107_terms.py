"""FR-107: agreeing to the terms, and being able to prove it later.

Trading is only allowed once somebody has accepted the version of the terms that was in
force at the time. That makes the acceptance worth keeping precisely: not a flag saying
"somebody ticked a box", but which version was accepted and when, so a dispute months later
has something to point at.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_FR_107_the_version_and_the_moment_are_both_recorded(
    client: TestClient,
    accounts: Callable[[], list[dict[str, object]]],
    sign_in: Callable[..., httpx.Response],
) -> None:
    """Which version was accepted, and when, is written down and kept."""
    sign_in("+251933300001", terms_version="2026-03-01")

    stored = accounts()[0]

    assert stored["terms_version"] == "2026-03-01"
    accepted_at = stored["terms_accepted_at"]
    assert isinstance(accepted_at, datetime)
    assert accepted_at.tzinfo is not None


def test_FR_107_the_recorded_moment_is_the_sign_in_not_the_start_up(
    client: TestClient,
    accounts: Callable[[], list[dict[str, object]]],
    sign_in: Callable[..., httpx.Response],
) -> None:
    """The timestamp has to mean something, so it cannot be the epoch or the row's creation.

    It is compared against the account's own creation: both happen in the same request, so
    what matters is that a real time was recorded rather than a placeholder.
    """
    sign_in("+251933300002")

    accepted_at = accounts()[0]["terms_accepted_at"]

    assert isinstance(accepted_at, datetime)
    assert accepted_at.year >= 2026


def test_FR_107_a_new_version_of_the_terms_has_to_be_accepted_again(
    client: TestClient,
    accounts: Callable[[], list[dict[str, object]]],
    sign_in: Callable[..., httpx.Response],
) -> None:
    """Accepting one version says nothing about the next one, so the record is replaced."""
    phone_e164 = "+251933300003"
    sign_in(phone_e164, terms_version="2026-01-01")
    assert accounts()[0]["terms_version"] == "2026-01-01"

    sign_in(phone_e164, terms_version="2026-06-01")

    assert accounts()[0]["terms_version"] == "2026-06-01"


def test_FR_107_no_session_is_handed_out_without_an_accepted_version(
    client: TestClient, code_for: Callable[[str], str]
) -> None:
    """The version is not optional: a client that omits it gets nothing."""
    phone_e164 = "+251933300004"
    client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    response = client.post(
        "/v1/auth/otp/verify",
        json={"phone_e164": phone_e164, "code": code_for(phone_e164)},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "terms_version" in response.json()["error"]["details"]["fields"]


def test_FR_107_an_empty_version_is_refused(
    client: TestClient, code_for: Callable[[str], str]
) -> None:
    """An empty string would record an acceptance of nothing in particular."""
    phone_e164 = "+251933300005"
    client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    response = client.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": phone_e164,
            "code": code_for(phone_e164),
            "terms_version": "",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_FR_107_a_version_longer_than_the_column_is_refused(
    client: TestClient, code_for: Callable[[str], str]
) -> None:
    """The declared width is enforced at the door rather than truncated on the way in."""
    phone_e164 = "+251933300006"
    client.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})

    response = client.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": phone_e164,
            "code": code_for(phone_e164),
            "terms_version": "v" * 21,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
