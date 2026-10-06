"""FR-402: the four orders a page may be in, and the tie-break that makes each one total.

The default is two things at once: nearest first when the trader has somewhere to measure
from, and newest first when they have not. The alternative orders need nothing from the
trader at all, which is why they are tested here without a location: a price list does not
become unavailable because somebody has not said where they stand.

Every order below ends in the listing's identity, and the tests deliberately create ties -
equal prices, equal harvest days - because an order that only looks right on distinct keys
is an order that repeats or drops rows the moment two sellers agree on a price, which they
do constantly.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

#: The point the location tests give a trader: inside woreda 2, so the nearest listing is
#: whichever of its own is being asked about, and the far woreda is a known trip away.
ADDIS = {"latitude": 9.03, "longitude": 38.75}


def _order(response: httpx.Response) -> list[str]:
    """The listings a page holds, in the order it holds them: these tests ask who is first."""
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["data"]]


def test_the_default_order_is_nearest_first_when_the_trader_has_a_location(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    woredas_with_boundaries: None,
) -> None:
    """Nearest first, then newest among the listings the radius left standing.

    Two listings share the nearer woreda, so distance alone cannot order them: the newest
    of the two has to come first, or the page would be answering with an order it cannot
    reproduce on the next request.
    """
    headers = signed_in()
    client.patch("/v1/me", headers=headers, json={"gps": ADDIS})
    near_older = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=2)).json()
    near_newer = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=2)).json()
    far = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=3)).json()

    default = client.get("/v1/search/listings", headers=headers)
    stretched = client.get("/v1/search/listings", headers=headers, params={"radius_km": "300"})
    stated = client.get(
        "/v1/search/listings",
        headers=headers,
        params={"radius_km": "300", "sort": "distance_asc"},
    )

    assert _order(default) == [near_newer["id"], near_older["id"]]
    assert _order(stretched) == [near_newer["id"], near_older["id"], far["id"]]
    assert _order(stated) == _order(stretched)


def test_the_default_order_without_a_location_is_newest_first(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """FR-402's other default: with nothing to measure from, time is the order.

    The first page is the newest thing posted, because that is the one order that needs
    nothing from the caller.
    """
    headers = signed_in()
    oldest = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    middle = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    newest = client.post("/v1/listings", headers=headers, json=listing_body()).json()

    response = client.get("/v1/search/listings", headers=headers)

    assert _order(response) == [newest["id"], middle["id"], oldest["id"]]


def test_price_ascending_puts_the_cheapest_first_and_breaks_a_tie_by_identity(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Two listings at the same price are an ordinary arrangement, not an edge case.

    Without the identity the page could not say which of the two comes first, and the next
    page would start in the middle of the tied group it had already begun showing.
    """
    headers = signed_in()
    dear = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=300_000)
    ).json()
    cheap_b = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=100_000)
    ).json()
    cheap_a = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=100_000)
    ).json()

    response = client.get("/v1/search/listings", headers=headers, params={"sort": "price_asc"})

    assert _order(response) == [*sorted([cheap_a["id"], cheap_b["id"]]), dear["id"]]


def test_price_descending_puts_the_dearest_first(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """The mirror of the ascending order, ties broken the same way so that walking two
    opposite orders over the same market visits every listing exactly once each."""
    headers = signed_in()
    cheap = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=100_000)
    ).json()
    dear_b = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=300_000)
    ).json()
    dear_a = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=300_000)
    ).json()

    response = client.get("/v1/search/listings", headers=headers, params={"sort": "price_desc"})

    assert _order(response) == [*sorted([dear_a["id"], dear_b["id"]]), cheap["id"]]


def test_harvest_date_ascending_puts_the_earliest_first(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """The harvest day a buyer would pick from first, ties by identity as everywhere else."""
    headers = signed_in()
    today = datetime.now(UTC).date()
    late = client.post(
        "/v1/listings",
        headers=headers,
        json=listing_body(harvest_date=(today + timedelta(days=28)).isoformat()),
    ).json()
    early = client.post(
        "/v1/listings",
        headers=headers,
        json=listing_body(harvest_date=(today + timedelta(days=7)).isoformat()),
    ).json()
    tied_one = client.post(
        "/v1/listings",
        headers=headers,
        json=listing_body(harvest_date=(today + timedelta(days=14)).isoformat()),
    ).json()
    tied_two = client.post(
        "/v1/listings",
        headers=headers,
        json=listing_body(harvest_date=(today + timedelta(days=14)).isoformat()),
    ).json()

    response = client.get(
        "/v1/search/listings", headers=headers, params={"sort": "harvest_date_asc"}
    )

    assert _order(response) == [
        early["id"],
        *sorted([tied_one["id"], tied_two["id"]]),
        late["id"],
    ]


def test_asking_for_the_measured_order_without_a_location_falls_back_to_newest(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Distance silently has nothing to measure when the trader gave no point.

    Refusing the request would be worse: the order was asked for by name, it is the
    default, and a trader without a landmark still has a newest-first market to read.
    """
    headers = signed_in()
    oldest = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    newest = client.post("/v1/listings", headers=headers, json=listing_body()).json()

    response = client.get("/v1/search/listings", headers=headers, params={"sort": "distance_asc"})

    assert _order(response) == [newest["id"], oldest["id"]]


def test_the_measured_distance_is_never_in_the_response(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    woredas_with_boundaries: None,
) -> None:
    """FR-404's line about place applies to measurements of it too.

    A distance is two traders' own points measured against each other, so it says where
    each of them stands - more than a woreda says, which is the finest place a listing is
    allowed to publish. The number exists only inside the statement and the cursor that
    carries a page; it is not a field of anything a caller can read.
    """
    headers = signed_in()
    client.patch("/v1/me", headers=headers, json={"gps": ADDIS})
    client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=2))

    response = client.get("/v1/search/listings", headers=headers)

    assert _order(response)
    for item in response.json()["data"]:
        assert "distance_m" not in item
        assert "distance" not in item
