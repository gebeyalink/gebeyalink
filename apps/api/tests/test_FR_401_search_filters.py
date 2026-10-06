"""FR-401: the filters a buyer may name, and the rule that combines them.

Each argument on its own is easy. The specification's real content sits in its last four
words - "All filters combine with AND" - because a search that ORed its arguments would
show every listing matching *something*, which on this market is nearly every listing.
So each test below arranges a listing that matches the argument under test and one that
does not, and the combination test arranges listings that each satisfy one argument but
not both.

The radius tests are the other half: a radius is a distance from a point the trader was
never asked for unless they gave it, so the same parameter means two different things
depending on whether they have a location, and both readings are checked here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

#: The point the radius tests give a trader: inside woreda 2, seventeen-odd kilometres from
#: nothing else that matters, and therefore unambiguous about which side of a fifty
#: kilometre radius a listing falls.
ADDIS = {"latitude": 9.03, "longitude": 38.75}


def _ids(response: httpx.Response) -> set[str]:
    """Which listings a page holds, as a set: these tests ask who is present."""
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["data"]}


def test_a_search_is_not_answered_for_an_unsigned_caller(client: TestClient) -> None:
    """What is on the market is still only for people who have said who they are."""
    assert client.get("/v1/search/listings").status_code == 401


def test_a_crop_filter_keeps_only_listings_of_that_crop(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """One crop, by the taxonomy's number, and nothing else in the page."""
    headers = signed_in()
    wheat = client.post("/v1/listings", headers=headers, json=listing_body(crop_id=2)).json()
    client.post("/v1/listings", headers=headers, json=listing_body(crop_id=1))

    response = client.get("/v1/search/listings", headers=headers, params={"crop_id": "2"})

    assert response.status_code == 200, response.text
    assert _ids(response) == {wheat["id"]}


def test_filters_combine_with_and_rather_than_or(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A crop and a claim about how it was grown, both of which have to hold.

    Three listings: one that satisfies both arguments, one that satisfies only the crop,
    one that satisfies only the claim. An OR would return all three, so this cannot pass
    for an implementation that got the combination backwards.
    """
    headers = signed_in()
    both = client.post(
        "/v1/listings", headers=headers, json=listing_body(crop_id=1, organic=True)
    ).json()
    client.post("/v1/listings", headers=headers, json=listing_body(crop_id=1, organic=False))
    client.post("/v1/listings", headers=headers, json=listing_body(crop_id=2, organic=True))

    response = client.get(
        "/v1/search/listings",
        headers=headers,
        params={"crop_id": "1", "organic": "true"},
    )

    assert response.status_code == 200, response.text
    assert _ids(response) == {both["id"]}


def test_a_region_filter_keeps_only_listings_from_that_region(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A region by its published two-letter code."""
    headers = signed_in()
    shewa = client.post("/v1/listings", headers=headers, json=listing_body(region_code="OR")).json()
    sidama = client.post(
        "/v1/listings", headers=headers, json=listing_body(region_code="SI")
    ).json()

    only_sidama = client.get("/v1/search/listings", headers=headers, params={"region_code": "SI"})
    only_shewa = client.get("/v1/search/listings", headers=headers, params={"region_code": "OR"})

    assert _ids(only_sidama) == {sidama["id"]}
    assert _ids(only_shewa) == {shewa["id"]}


def test_a_region_that_is_not_one_of_the_thirteen_is_refused(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """Refused rather than treated as a region with nothing in it.

    A mistyped code returning an empty page looks exactly like a region where nothing is
    for sale, and only one of those is a bug the caller could notice.
    """
    response = client.get("/v1/search/listings", headers=signed_in(), params={"region_code": "XX"})

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_woreda_filter_keeps_only_listings_from_that_woreda(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    woredas_with_boundaries: None,
) -> None:
    """One woreda, by its internal id."""
    headers = signed_in()
    adaa = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=1)).json()
    fentale = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=2)).json()

    only_fentale = client.get("/v1/search/listings", headers=headers, params={"woreda_id": "2"})
    only_adaa = client.get("/v1/search/listings", headers=headers, params={"woreda_id": "1"})

    assert _ids(only_fentale) == {fentale["id"]}
    assert _ids(only_adaa) == {adaa["id"]}


def test_a_price_range_keeps_only_prices_inside_it(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Both ends belong to the range: a price equal to the bound is inside it.

    Rejection at the boundary rather than somewhere near it is the difference between a
    buyer who sees their own listing and one who wonders why it vanished.
    """
    headers = signed_in()
    cheap = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=100_000)
    ).json()
    middle = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=200_000)
    ).json()
    dear = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=300_000)
    ).json()

    between = client.get(
        "/v1/search/listings",
        headers=headers,
        params={"price_min": "150000", "price_max": "250000"},
    )
    from_middle_up = client.get(
        "/v1/search/listings", headers=headers, params={"price_min": "200000"}
    )
    up_to_middle = client.get(
        "/v1/search/listings", headers=headers, params={"price_max": "200000"}
    )

    assert _ids(between) == {middle["id"]}
    assert _ids(from_middle_up) == {middle["id"], dear["id"]}
    assert _ids(up_to_middle) == {cheap["id"], middle["id"]}


def test_a_harvest_window_keeps_only_harvests_inside_it(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A window over the harvest day, both ends included."""
    headers = signed_in()
    today = datetime.now(UTC).date()
    posted = {
        days: client.post(
            "/v1/listings",
            headers=headers,
            json=listing_body(harvest_date=(today + timedelta(days=days)).isoformat()),
        ).json()["id"]
        for days in (7, 14, 28)
    }

    window = client.get(
        "/v1/search/listings",
        headers=headers,
        params={
            "harvest_date_from": (today + timedelta(days=7)).isoformat(),
            "harvest_date_to": (today + timedelta(days=14)).isoformat(),
        },
    )

    assert _ids(window) == {posted[7], posted[14]}


def test_a_delivery_filter_keeps_only_listings_offering_it(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A delivery method the listing offers, and no listing that does not.

    The arrangement each listing needs to be valid at all is part of the setup: seller
    delivery carries a fee and every other method does not, so a listing built the other
    way would be refused at posting and never reach a search to begin with.
    """
    headers = signed_in()
    pickup = client.post(
        "/v1/listings",
        headers=headers,
        json=listing_body(delivery_options=["PICKUP"], delivery_fee_santim=0),
    ).json()
    delivered = client.post(
        "/v1/listings",
        headers=headers,
        json=listing_body(delivery_options=["SELLER_DELIVERY"], delivery_fee_santim=400),
    ).json()
    carried = client.post(
        "/v1/listings",
        headers=headers,
        json=listing_body(delivery_options=["TRANSPORT_PARTNER"], delivery_fee_santim=0),
    ).json()

    by_pickup = client.get(
        "/v1/search/listings", headers=headers, params={"delivery_option": "PICKUP"}
    )
    by_transport = client.get(
        "/v1/search/listings", headers=headers, params={"delivery_option": "TRANSPORT_PARTNER"}
    )
    by_seller = client.get(
        "/v1/search/listings", headers=headers, params={"delivery_option": "SELLER_DELIVERY"}
    )

    assert _ids(by_pickup) == {pickup["id"]}
    assert _ids(by_seller) == {delivered["id"]}
    assert _ids(by_transport) == {carried["id"]}


def test_a_seller_tier_filter_keeps_only_sellers_at_or_above_it(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    postgres_url: str,
) -> None:
    """A floor rather than an equality: TIER_1 asks for the checked sellers and everyone
    above them, because an account that proved more is not thereby less trustworthy.

    The tiers are written onto the accounts directly because no journey in this slice
    moves an account between them; the filter reads whatever the account says.
    """
    numbers = ("+251911000001", "+251911000002", "+251911000003")
    tiers = ("TIER_0", "TIER_1", "TIER_2")
    headers = [signed_in(phone) for phone in numbers]
    posted = [
        client.post("/v1/listings", headers=caller, json=listing_body()).json()
        for caller in headers
    ]
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        for phone, tier in zip(numbers, tiers, strict=True):
            connection.execute(
                "UPDATE users SET kyc_tier = %s WHERE phone_e164 = %s", (tier, phone)
            )

    at_least_1 = client.get(
        "/v1/search/listings", headers=headers[0], params={"seller_tier_min": "TIER_1"}
    )
    at_least_2 = client.get(
        "/v1/search/listings", headers=headers[0], params={"seller_tier_min": "TIER_2"}
    )
    everybody = client.get(
        "/v1/search/listings", headers=headers[0], params={"seller_tier_min": "TIER_0"}
    )

    assert _ids(at_least_1) == {posted[1]["id"], posted[2]["id"]}
    assert _ids(at_least_2) == {posted[2]["id"]}
    assert _ids(everybody) == {posted[0]["id"], posted[1]["id"], posted[2]["id"]}


def test_two_ranges_that_contradict_each_other_answer_with_nothing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A minimum above a maximum matches no row, and that is an answer, not an error.

    The caller asked which listings cost between here and there; none do. Refusing the
    question would tell the caller their request was malformed when it was merely empty.
    """
    headers = signed_in()
    client.post("/v1/listings", headers=headers, json=listing_body(unit_price_santim=200_000))

    response = client.get(
        "/v1/search/listings",
        headers=headers,
        params={"price_min": "300000", "price_max": "100000"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"data": [], "next_cursor": None, "has_more": False}


def test_a_radius_without_a_location_of_your_own_is_refused(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Refused rather than ignored.

    A trader who asked to search within fifty kilometres and is handed the whole country
    has been given an answer to a question they did not ask, with no way to notice.
    """
    headers = signed_in()
    client.post("/v1/listings", headers=headers, json=listing_body())

    response = client.get("/v1/search/listings", headers=headers, params={"radius_km": "50"})

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"]["radius_km"] == "requires_your_location"


@pytest.mark.parametrize("radius", ["0", "501"])
def test_a_radius_outside_the_stated_range_is_refused(
    client: TestClient, signed_in: Callable[..., dict[str, str]], radius: str
) -> None:
    """Refused rather than clamped, as every bound on this API is: a client asking for
    zero has a bug, and answering it with the default would hide it."""
    response = client.get("/v1/search/listings", headers=signed_in(), params={"radius_km": radius})

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_radius_keeps_only_woredas_within_it(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    woredas_with_boundaries: None,
) -> None:
    """Two mapped woredas a hundred and eighty kilometres apart, and one nobody has mapped.

    The unmapped one is the point of the third listing: a missing boundary is not a
    distance of zero and not a distance of everything, it is no distance at all, and a
    radius that kept it would be claiming a boundary the platform does not have.
    """
    headers = signed_in()
    client.patch("/v1/me", headers=headers, json={"gps": ADDIS})
    near = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=2)).json()
    far = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=3)).json()
    unmapped = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=4)).json()

    within_fifty = client.get("/v1/search/listings", headers=headers, params={"radius_km": "50"})
    within_three_hundred = client.get(
        "/v1/search/listings", headers=headers, params={"radius_km": "300"}
    )

    assert _ids(within_fifty) == {near["id"]}
    assert _ids(within_three_hundred) == {near["id"], far["id"]}
    assert unmapped["id"] not in _ids(within_three_hundred)


def test_the_radius_falls_back_to_fifty_kilometres_when_the_trader_has_a_location(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    woredas_with_boundaries: None,
) -> None:
    """FR-401's default radius, applied only where there is something to measure from.

    The default is not a promise to ignore distance: a trader with a point who said
    nothing about range still asked a question about range, and fifty kilometres is the
    specification's answer.
    """
    headers = signed_in()
    client.patch("/v1/me", headers=headers, json={"gps": ADDIS})
    near = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=2)).json()
    client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=3)).json()

    response = client.get("/v1/search/listings", headers=headers)

    assert _ids(response) == {near["id"]}


def test_a_trader_without_a_location_is_not_filtered_by_distance(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    woredas_with_boundaries: None,
) -> None:
    """Distance silently has nothing to say about a trader who gave none.

    No point means no radius, not a default one: the same request from the same trader
    after they have said where they are would be a different question, and this one never
    mentioned where they are.
    """
    headers = signed_in()
    near = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=2)).json()
    far = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=3)).json()
    unmapped = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=4)).json()

    response = client.get("/v1/search/listings", headers=headers)

    assert _ids(response) == {near["id"], far["id"], unmapped["id"]}


def test_a_radius_still_combines_with_the_other_filters(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    woredas_with_boundaries: None,
) -> None:
    """Both arguments have to hold, and neither is allowed to answer for the other.

    The woreda named is outside the radius asked for, so the honest page is empty; an
    implementation that let one argument win would show it anyway.
    """
    headers = signed_in()
    client.patch("/v1/me", headers=headers, json={"gps": ADDIS})
    client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=2))
    far = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=3)).json()

    named_but_distant = client.get(
        "/v1/search/listings",
        headers=headers,
        params={"radius_km": "50", "woreda_id": "3"},
    )
    named_and_within = client.get(
        "/v1/search/listings",
        headers=headers,
        params={"radius_km": "300", "woreda_id": "3"},
    )

    assert named_but_distant.json() == {"data": [], "next_cursor": None, "has_more": False}
    assert _ids(named_and_within) == {far["id"]}
