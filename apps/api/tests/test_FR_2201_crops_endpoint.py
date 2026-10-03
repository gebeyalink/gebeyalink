"""The crops endpoint (SPEC 12, 27.2, FR-2201, FR-2508).

A listing states a weight and a unit. Only crops measured in kilograms can carry that, so
anything counted by head is deliberately not offered here. Each crop also carries how long an
escrow on it is held, because grain and leafy produce do not keep for the same time.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def access_token(sign_in: Callable[..., httpx.Response]) -> str:
    """Sign a trader in and keep the access token their session was issued."""
    verified = sign_in("+251911000001", terms_version="2026-01-01")
    assert verified.status_code == 200, verified.text
    return str(verified.json()["access_token"])


def _list(client: TestClient, token: str) -> list[dict[str, object]]:
    response = client.get("/v1/crops", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    body: list[dict[str, object]] = response.json()
    return body


def test_crops_require_a_signed_in_caller(client: TestClient) -> None:
    response = client.get("/v1/crops")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_only_active_crops_measured_in_kilograms_are_listed(
    client: TestClient, access_token: str
) -> None:
    listed = _list(client, access_token)
    assert len(listed) == 19
    assert all(crop["active"] is True for crop in listed)
    assert all(crop["base_unit"] == "KG" for crop in listed)


def test_livestock_is_not_offered_as_a_crop(client: TestClient, access_token: str) -> None:
    listed = _list(client, access_token)
    assert all(crop["name_en"] != "Cattle (live)" for crop in listed)
    assert all(crop["base_unit"] != "UNIT" for crop in listed)


def test_every_crop_carries_the_names_that_have_been_reviewed(
    client: TestClient, access_token: str
) -> None:
    """Oromo is part of the contract but has no sourced names yet, so it arrives empty."""
    for crop in _list(client, access_token):
        assert crop["name_en"] and crop["name_am"]
        assert crop["name_om"] is None


def test_crops_carry_the_taxonomy_number_and_how_long_an_escrow_is_held(
    client: TestClient, access_token: str
) -> None:
    """A listing needs both to be written: which crop, and how long the money is held."""
    listed = _list(client, access_token)
    assert [crop["id"] for crop in listed] == list(range(1, 20))
    for crop in listed:
        held = crop["escrow_hold_hours"]
        assert isinstance(held, int) and held > 0


def test_tigrinya_is_stored_but_not_offered(client: TestClient, access_token: str) -> None:
    """The column exists for a future pilot; this contract does not include it."""
    for crop in _list(client, access_token):
        assert "name_ti" not in crop


def test_a_truncated_access_token_is_refused(client: TestClient, access_token: str) -> None:
    response = client.get("/v1/crops", headers={"Authorization": f"Bearer {access_token[:-4]}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TOKEN_INVALID"
