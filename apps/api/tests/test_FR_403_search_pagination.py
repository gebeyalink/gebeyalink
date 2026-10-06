"""FR-403: pages that turn by cursor, and the boundaries a cursor has to carry.

Offset pagination is forbidden, and the tests below check that the way it is forbidden is
by having no effect at all: a caller who sends one gets the same page they would have got
without it, so no client can be relying on it and no server can be quietly honouring it.

The rest is about the cursor as a promise. It has to be a token this server wrote, it has
to name the order that produced it, and it has to land exactly where the last page ended -
which is why AC-4.1 forces every listing to the same instant before walking, so that time
cannot be doing the work of keeping pages apart and only the identity can.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable

import psycopg
import pytest
from fastapi.testclient import TestClient

# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

#: The point the distance walk gives a trader: inside woreda 2, which is where the two
#: equal-distance listings stand.
ADDIS = {"latitude": 9.03, "longitude": 38.75}

#: Every listing the AC's fifty are posted at the same instant, so the order has nothing
#: to stand on except the identity the tie-break ends in.
TIED_INSTANT = "2026-01-15T12:00:00+00:00"


def test_AC_4_1_fifty_matching_listings_come_back_exactly_once(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    postgres_url: str,
) -> None:
    """The acceptance case: fifty listings, pages of twenty, no duplicates and no gaps.

    All fifty are stamped with the same ``created_at`` first, because posting them takes
    real time and time alone would then separate the pages. With the clock holding still
    the only thing left to order them by is the identity, so a cursor that repeats or
    drops a row has nowhere to hide.
    """
    headers = signed_in()
    posted = [
        client.post("/v1/listings", headers=headers, json=listing_body()).json()["id"]
        for _ in range(50)
    ]
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("UPDATE listings SET created_at = %s", (TIED_INSTANT,))

    first = client.get("/v1/search/listings", headers=headers)
    assert first.status_code == 200, first.text
    opening = first.json()
    assert len(opening["data"]) == 20
    assert opening["has_more"] is True
    assert opening["next_cursor"] is not None

    collected: list[str] = [item["id"] for item in opening["data"]]
    cursor = opening["next_cursor"]
    while cursor is not None:
        page = client.get(
            "/v1/search/listings", headers=headers, params={"limit": "20", "cursor": cursor}
        )
        assert page.status_code == 200, page.text
        body = page.json()
        collected.extend(item["id"] for item in body["data"])
        cursor = body["next_cursor"]
        assert len(collected) <= 100, "the walk is not converging"

    assert len(collected) == 50
    assert len(set(collected)) == 50
    assert set(collected) == set(posted)
    # Every listing written at the same instant falls back to the identity, newest-declared
    # first in the database's own order; the walk has to reproduce exactly that order.
    assert collected == sorted(posted, reverse=True)


def test_a_cursor_carries_the_order_it_was_issued_under(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Paging with nothing but a cursor keeps the order the first page was in.

    The second page restates the order once - a client may do that, and it must agree -
    and the third page says nothing at all, which is the ordinary case: a phone walking
    results does not re-derive why they were sorted the way they were.
    """
    headers = signed_in()
    dearest = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=400_000)
    ).json()
    dear = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=300_000)
    ).json()
    middle = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=200_000)
    ).json()
    cheap_b = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=100_000)
    ).json()
    cheap_a = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=100_000)
    ).json()
    expected = [
        *sorted([cheap_a["id"], cheap_b["id"]]),
        middle["id"],
        dear["id"],
        dearest["id"],
    ]

    first = client.get(
        "/v1/search/listings",
        headers=headers,
        params={"sort": "price_asc", "limit": "2"},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert [item["id"] for item in first_body["data"]] == expected[:2]

    second = client.get(
        "/v1/search/listings",
        headers=headers,
        params={"sort": "price_asc", "limit": "2", "cursor": first_body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert [item["id"] for item in second_body["data"]] == expected[2:4]

    third = client.get(
        "/v1/search/listings",
        headers=headers,
        params={"limit": "2", "cursor": second_body["next_cursor"]},
    )
    assert third.status_code == 200, third.text
    third_body = third.json()
    assert [item["id"] for item in third_body["data"]] == expected[4:]
    assert third_body["has_more"] is False
    assert third_body["next_cursor"] is None


def test_an_order_stated_that_contradicts_the_cursor_is_refused(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A cursor means one thing only under the order that produced it.

    Adopting the stated order instead would hand back a page under an order its cursor
    does not describe; refusing says which of the two the client got wrong.
    """
    headers = signed_in()
    client.post("/v1/listings", headers=headers, json=listing_body(unit_price_santim=100_000))
    client.post("/v1/listings", headers=headers, json=listing_body(unit_price_santim=50_000))
    first = client.get(
        "/v1/search/listings", headers=headers, params={"sort": "price_asc", "limit": "1"}
    )
    assert first.json()["next_cursor"] is not None, "a page of one of two needs a cursor"

    contradiction = client.get(
        "/v1/search/listings",
        headers=headers,
        params={"sort": "price_desc", "cursor": first.json()["next_cursor"]},
    )

    assert contradiction.status_code == 422, contradiction.text
    error = contradiction.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"]["sort"] == "does_not_match_the_cursor"


def test_a_cursor_this_server_did_not_write_is_refused(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """A cursor is an opaque token the server issued; anything else is a client bug.

    Guessing at one would mean reconstructing an ordering that was never part of one, and
    a page built from it could quietly skip listings nobody had been shown yet.
    """
    response = client.get(
        "/v1/search/listings", headers=signed_in(), params={"cursor": "not-a-cursor"}
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"]["cursor"] == "not_a_valid_cursor"


def test_a_cursor_missing_the_part_its_order_needs_is_refused(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """A boundary that claims to end at a price it did not carry is not a boundary.

    Well-formed and empty are different failures from malformed: this token parses, it
    even names an order, and what it leaves out is exactly what that order measures by.
    """
    raw = f"price_asc|{TIED_INSTANT}|{uuid.uuid4()}||2026-06-01|"
    cursor = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    response = client.get("/v1/search/listings", headers=signed_in(), params={"cursor": cursor})

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"]["cursor"] == "missing_unit_price_santim"


@pytest.mark.parametrize("limit", ["0", "-1", "101"])
def test_a_limit_outside_the_stated_range_is_refused(
    client: TestClient, signed_in: Callable[..., dict[str, str]], limit: str
) -> None:
    """Refused rather than clamped.

    A client asking for zero has a bug, and answering it with a default page would hide
    that bug until somebody wondered why their results looked full.
    """
    response = client.get("/v1/search/listings", headers=signed_in(), params={"limit": limit})

    assert response.status_code == 422, response.text


def test_a_limit_of_the_ceiling_is_answered(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """The ceiling is accepted as a request, not only refused as a bound."""
    headers = signed_in()
    client.post("/v1/listings", headers=headers, json=listing_body())
    client.post("/v1/listings", headers=headers, json=listing_body())

    response = client.get("/v1/search/listings", headers=headers, params={"limit": "100"})

    assert response.status_code == 200, response.text
    assert len(response.json()["data"]) == 2


def test_an_offset_sends_back_the_same_page_it_would_have_gotten_anyway(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Offset pagination is forbidden by FR-403, and this is what forbidden means here.

    The parameter is not in the contract, so it is not parsed, so it cannot shift the
    window: identical requests with and without it answer with identical pages. A test
    only asserting the parameter were *documented* would pass against an implementation
    that quietly honoured it.
    """
    headers = signed_in()
    for _ in range(3):
        client.post("/v1/listings", headers=headers, json=listing_body())

    without = client.get("/v1/search/listings", headers=headers)
    with_offset = client.get("/v1/search/listings", headers=headers, params={"offset": "2"})

    assert without.status_code == 200, without.text
    assert with_offset.status_code == 200, with_offset.text
    assert with_offset.json() == without.json()


def test_pages_walked_under_the_measured_order_land_on_each_listing_exactly_once(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    woredas_with_boundaries: None,
) -> None:
    """The measured order from inside a page: two listings a like, one further off.

    The two near woreda 2 are the whole difficulty - same distance, so the boundary has to
    carry the measurement *and* the time to tell them apart, and the next page has to
    continue from a distance it was handed rather than one it can recompute from anything
    in the response.
    """
    headers = signed_in()
    client.patch("/v1/me", headers=headers, json={"gps": ADDIS})
    near_older = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=2)).json()
    near_newer = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=2)).json()
    far = client.post("/v1/listings", headers=headers, json=listing_body(woreda_id=3)).json()
    expected = [near_newer["id"], near_older["id"], far["id"]]

    collected: list[str] = []
    cursor: str | None = None
    for _ in range(len(expected)):
        params: dict[str, str] = {"radius_km": "300", "limit": "1"}
        if cursor is not None:
            params["cursor"] = cursor
        page = client.get("/v1/search/listings", headers=headers, params=params)
        assert page.status_code == 200, page.text
        body = page.json()
        collected.extend(item["id"] for item in body["data"])
        cursor = body["next_cursor"]
        if not body["has_more"]:
            break

    assert collected == expected
    assert cursor is None


def test_an_empty_market_answers_with_an_empty_page(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """An empty list is a page with nothing in it, not a missing resource."""
    response = client.get("/v1/search/listings", headers=signed_in())

    assert response.status_code == 200, response.text
    assert response.json() == {"data": [], "next_cursor": None, "has_more": False}
