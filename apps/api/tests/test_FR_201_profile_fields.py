"""The profile fields a trader sends, and the ones that come back.

What is being pinned down here is which fields exist and what shape they hold, not the rules
about which values are acceptable: those are their own tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient


def test_a_fresh_account_answers_with_nulls_rather_than_asking(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """Reading before writing is a normal state, not a prompt to fill something in.

    Somebody who has only ever signed in has nothing to say about themselves, and the answer
    says so with nulls rather than refusing or inventing values.
    """
    headers = signed_in()
    response = client.get("/v1/me", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    for field in (
        "display_name",
        "region_code",
        "woreda_id",
        "kebele",
        "business_name",
        "license_no",
        "tax_identification_number",
    ):
        assert body[field] is None, f"{field} should be null before it is ever written"


def test_the_response_carries_the_account_alongside_the_profile(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """The profile is the account's own details, not a separate thing to fetch.

    A caller asking who they are should not have to join two responses to find out.
    """
    headers = signed_in()
    client.patch("/v1/me", headers=headers, json={"display_name": "Abebe Bekele"})

    body = client.get("/v1/me", headers=headers).json()

    assert body["display_name"] == "Abebe Bekele"
    assert body["phone_e164"] == "+251911000001"
    assert body["role"] == "FARMER"
    assert body["kyc_tier"] == "TIER_0"
    assert body["status"] == "ACTIVE"
    assert body["terms_version"] is not None
    assert body["language"] in {"am", "om", "en"}


def test_the_response_always_lists_the_activities_a_trader_holds(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
) -> None:
    """A trader holds exactly the one activity they signed up for, and that is said plainly.

    The list is present and empty rather than missing, because a client written against the
    response shape should not have to handle its absence to discover there is nothing extra
    to show.
    """
    headers = signed_in()

    fresh = client.get("/v1/me", headers=headers).json()
    assert fresh["additional_roles"] == []

    client.patch("/v1/me", headers=headers, json={"display_name": "Abebe Bekele"})
    after_write = client.get("/v1/me", headers=headers).json()
    assert after_write["additional_roles"] == []


def test_the_primary_activity_is_not_also_reported_as_an_additional_one(
    client: TestClient, signed_in_as_buyer: Callable[..., dict[str, str]]
) -> None:
    """A buyer's buying is their primary activity, not something held on the side.

    Worth pinning down because reporting the same activity twice would look correct to a
    client and would put the trader in a list they never asked to join.
    """
    headers = signed_in_as_buyer("+251911000004")

    body = client.get("/v1/me", headers=headers).json()

    assert body["role"] == "BUYER"
    assert "BUYER" not in body["additional_roles"]


def test_every_profile_field_survives_a_write_and_a_read(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    profiles: Callable[[], list[dict[str, Any]]],
) -> None:
    """A trader can describe themselves fully, and find it all still there.

    Read back out of the database rather than only through the endpoint, so a field that was
    accepted and then quietly dropped would still be caught here.
    """
    headers = signed_in()
    sent: dict[str, Any] = {
        "display_name": "Almaz Tesfaye",
        "region_code": "AA",
        "woreda_id": 1,
        "kebele": "Bole",
        "business_name": "Tesfaye Fresh Produce",
        "license_no": "BL-2026-0042",
        "tax_identification_number": "0001234567",
        "language": "om",
    }

    response = client.patch("/v1/me", headers=headers, json=sent)
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["display_name"] == sent["display_name"]
    assert body["region_code"] == "AA"
    assert body["woreda_id"] == 1
    assert body["kebele"] == "Bole"
    assert body["business_name"] == sent["business_name"]
    assert body["license_no"] == sent["license_no"]
    assert body["tax_identification_number"] == sent["tax_identification_number"]
    assert body["language"] == "om"

    stored = profiles()[0]
    assert stored["display_name"] == "Almaz Tesfaye"
    assert stored["region_code"] == "AA"
    assert stored["woreda_id"] == 1
    assert stored["kebele"] == "Bole"
    assert stored["business_name"] == "Tesfaye Fresh Produce"
    assert stored["license_no"] == "BL-2026-0042"
    assert stored["tax_identification_number"] == "0001234567"


def test_the_language_choice_is_remembered_across_requests(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """A trader's language is a decision they made, not a guess from a header.

    Read back on a later, separate request so the test proves it was stored rather than
    echoed from the request that set it.
    """
    headers = signed_in()
    client.patch("/v1/me", headers=headers, json={"language": "en"})

    assert client.get("/v1/me", headers=headers).json()["language"] == "en"


def test_an_account_has_at_most_one_profile(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    profiles: Callable[[], list[dict[str, Any]]],
) -> None:
    """One account, one profile.

    The uniqueness is enforced by the database rather than by the endpoint, so repeated
    writes add rows to the same profile instead of starting a second one.
    """
    headers = signed_in()
    client.patch("/v1/me", headers=headers, json={"display_name": "First Name"})
    client.patch("/v1/me", headers=headers, json={"kebele": "Kirkos"})

    assert len(profiles()) == 1


def test_reading_a_profile_needs_a_token(client: TestClient) -> None:
    """Nobody's profile is public, so an unauthenticated read is refused."""
    response = client.get("/v1/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_changing_a_profile_needs_a_token(client: TestClient) -> None:
    """And neither is changing one."""
    response = client.patch("/v1/me", json={"display_name": "Nobody At All"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_a_bogus_token_is_refused_rather_than_treated_as_signed_out(
    client: TestClient,
) -> None:
    """A token that cannot be read is a different failure from no token at all.

    The distinction matters to a client deciding whether to sign in again or to try the same
    request once more.
    """
    response = client.get("/v1/me", headers={"Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TOKEN_INVALID"


def test_one_trader_cannot_read_another_traders_profile(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """Each caller gets their own profile, and only their own.

    There is no path parameter to aim at somebody else, which is the point: the token says
    whose profile this is.
    """
    first = signed_in("+251911000001")
    client.patch("/v1/me", headers=first, json={"display_name": "Abebe Bekele"})

    second = signed_in("+251911000002")

    body = client.get("/v1/me", headers=second).json()
    assert body["display_name"] is None
    assert body["phone_e164"] == "+251911000002"
