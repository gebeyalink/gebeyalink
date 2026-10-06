"""FR-404: what the market is, and what a stranger is never told about who is in it.

The rules both face the same direction. A status the seller reached by their own hand -
pausing, closing - takes a listing straight out of the results, and so does a state behind
the API that no search could ever have served: a draft was never published, an expired row
outlived its clock, a suspended one is a platform's ruling and not a seller's. Sold out is
the one exception named in the contract, and it is tested as one.

The other half is about the shape of an item. Somebody searching knows the woreda a crop
stands at, because it is where they go to collect it; they are never told the exact spot,
the seller's own account, or the measure the server sorted them by, so a result carries
exactly the fields of the listing it points at, nothing more.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

# SPEC §20: these tests search the real store through the client, so they are integration.
pytestmark = pytest.mark.integration


def _ids(body: dict[str, Any]) -> list[str]:
    """The ids of the page, in the order they came off the wire."""
    return [item["id"] for item in body["data"]]


def test_the_states_that_stay_out_of_the_results_are_exactly_the_specs(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    postgres_url: str,
) -> None:
    """Draft, paused, expired, suspended, closed; nothing else is refused.

    Each exclusion is a different kind of absence, and a test that lumped them together
    could pass on any one of them failing. Pausing and closing are the seller's own acts,
    done through the API; the other three are states the API never writes and a search
    caller never asked for, so they are written straight to the row the way the state table
    itself would recall them.
    """
    headers = signed_in()
    control = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    to_pause = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    to_close = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    to_draft = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    to_expire = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    to_suspend = client.post("/v1/listings", headers=headers, json=listing_body()).json()

    assert client.post(f"/v1/listings/{to_pause['id']}/pause", headers=headers).status_code == 200
    assert client.post(f"/v1/listings/{to_close['id']}/close", headers=headers).status_code == 200
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("UPDATE listings SET status = 'DRAFT' WHERE id = %s", (to_draft["id"],))
        connection.execute(
            "UPDATE listings SET status = 'EXPIRED' WHERE id = %s", (to_expire["id"],)
        )
        connection.execute(
            "UPDATE listings SET status = 'SUSPENDED' WHERE id = %s", (to_suspend["id"],)
        )

    response = client.get("/v1/search/listings", headers=headers)

    assert response.status_code == 200, response.text
    assert _ids(response.json()) == [control["id"]]


def test_a_listing_whose_expiry_has_passed_is_gone_even_though_its_row_says_active(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    lapse_listing: Callable[[str], None],
) -> None:
    """An active row that outlived its clock is no longer on the market.

    ``expires_at`` is a promise about the future, and the market reads the clock. The row
    still says ACTIVE because that is what its seller last chose; the search is what has to
    remember what ACTIVE means.
    """
    headers = signed_in()
    control = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    lapsed = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    lapse_listing(lapsed["id"])

    response = client.get("/v1/search/listings", headers=headers)

    assert response.status_code == 200, response.text
    assert _ids(response.json()) == [control["id"]]


def test_a_sold_out_listing_stays_where_buyers_can_still_see_it(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    postgres_url: str,
) -> None:
    """Sold out is off the table, not off the market.

    FR-404 lists it among the states search has to show: a buyer's next question is what
    else the seller has, and a listing that answered a query an hour ago does not get to
    stop answering it because every bag is spoken for.
    """
    headers = signed_in()
    sold_out = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute(
            "UPDATE listings SET status = 'SOLD_OUT' WHERE id = %s", (sold_out["id"],)
        )

    response = client.get("/v1/search/listings", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["data"]] == [sold_out["id"]]
    assert body["data"][0]["status"] == "SOLD_OUT"


def test_a_sold_out_listing_is_still_offered_after_its_expiry_passes(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    postgres_url: str,
    lapse_listing: Callable[[str], None],
) -> None:
    """For a sold-out listing, expiry turns off the clock instead of the listing.

    The attention a sold-out listing still earns is directed at the seller, so it is the
    listing that has to be findable; whether it could still be *sold* stops mattering the
    moment the last bag left. The expiry that dismisses an active row must not be the one
    that dismisses a sold-out one.
    """
    headers = signed_in()
    control = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    sold_out = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute(
            "UPDATE listings SET status = 'SOLD_OUT' WHERE id = %s", (sold_out["id"],)
        )
    lapse_listing(sold_out["id"])

    response = client.get("/v1/search/listings", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert {item["id"] for item in body["data"]} == {control["id"], sold_out["id"]}
    assert body["data"][0]["status"] == "SOLD_OUT"


def test_a_result_names_the_place_it_stands_and_nothing_about_the_person_behind_it(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    woredas_with_boundaries: None,
) -> None:
    """The woreda is out in the open because it is the meeting place; the rest is not.

    The platform takes a farmer's exact spot - and a name and kebele along with it - so it
    can answer a radius, and it answers one without ever passing any of that on. A buyer
    searching sees the woreda the crop stands at, which is where they collect it, and no
    field at all about the seller. The item is also not asked to explain how it got its
    place on the page, so the measured distance stays off it.
    """
    headers = signed_in()
    client.patch(
        "/v1/me",
        headers=headers,
        json={
            "display_name": "Abebe Kebede",
            "kebele": "Batu 03",
            "gps": {"latitude": 9.03, "longitude": 38.75},
        },
    )
    listed = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=2)).json()

    response = client.get("/v1/search/listings", headers=headers, params={"radius_km": "50"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"data", "next_cursor", "has_more"}
    assert len(body["data"]) == 1
    item = body["data"][0]
    assert item["id"] == listed["id"]
    assert item["region_code"] == "OR"
    assert item["woreda_id"] == 2
    for field in (
        "kebele",
        "gps",
        "latitude",
        "longitude",
        "seller_id",
        "on_behalf_of_user_id",
        "distance_m",
    ):
        assert field not in item, f"{field} leaked into a search result"


def test_a_search_result_and_the_listing_it_points_at_say_the_same_thing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A hit is the listing, not a summary of it.

    Matching the detail view field for field means a buyer who taps a result gets the very
    listing they tapped, and an app that renders either has only one shape to learn.
    """
    headers = signed_in()
    posted = client.post("/v1/listings", headers=headers, json=listing_body()).json()

    response = client.get("/v1/search/listings", headers=headers)
    assert response.status_code == 200, response.text
    item = response.json()["data"][0]

    assert item == client.get(f"/v1/listings/{posted['id']}", headers=headers).json()
    assert item["available_kg"] == item["quantity_kg"]
