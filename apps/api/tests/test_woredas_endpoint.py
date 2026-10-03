"""The woredas endpoint (SPEC §11.1).

A trader trades from somewhere specific, so the list is normally narrowed to their region.
Leaving the region out is answered rather than refused.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("migrated_database")


@pytest.fixture()
def access_token(sign_in: Callable[..., httpx.Response]) -> str:
    verified = sign_in("+251911000002", terms_version="2026-01-01")
    assert verified.status_code == 200, verified.text
    return str(verified.json()["access_token"])


def _get(client: TestClient, token: str, query: str = "") -> list[dict[str, object]]:
    response = client.get(f"/v1/woredas{query}", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    body: list[dict[str, object]] = response.json()
    return body


def test_woredas_require_a_signed_in_caller(client: TestClient) -> None:
    response = client.get("/v1/woredas")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_a_region_filter_returns_only_that_region(client: TestClient, access_token: str) -> None:
    listed = _get(client, access_token, "?region_code=OR")
    assert listed
    assert all(woreda["region_code"] == "OR" for woreda in listed)


def test_a_region_filter_includes_the_pilot_woreda(client: TestClient, access_token: str) -> None:
    listed = _get(client, access_token, "?region_code=OR")
    assert [woreda["name"] for woreda in listed] == ["Adaa"]


def test_a_region_with_no_woredas_is_answered_with_an_empty_list(
    client: TestClient, access_token: str
) -> None:
    assert _get(client, access_token, "?region_code=TI") == []


def test_leaving_the_region_out_returns_every_seeded_woreda(
    client: TestClient, access_token: str
) -> None:
    listed = _get(client, access_token)
    assert len(listed) == 1
    assert listed[0]["name"] == "Adaa"


def test_the_pilot_woreda_is_answered_with_the_fields_the_contract_names(
    client: TestClient, access_token: str
) -> None:
    listed = _get(client, access_token, "?region_code=OR")
    assert listed[0]["name"] == "Adaa"
    assert listed[0]["zone"] == "East Shewa"


def test_a_woreda_boundary_is_not_published_through_the_api(
    client: TestClient, access_token: str
) -> None:
    """A boundary is a whole polygon; the contract does not ask for it here."""
    listed = _get(client, access_token, "?region_code=OR")
    assert "geog" not in listed[0]
