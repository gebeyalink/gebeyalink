"""A price compared with what that crop last cost in that region.

The flag is the whole of it. A price that looks odd is worth saying out loud to the seller
and to whoever reads the listing afterwards, and is never worth refusing the sale over: the
seller may know something about their own crop that an average does not.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest
from fastapi.testclient import TestClient

# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

#: What teff went for in Oromia, and what the tests price against.
AVERAGE_SANTIM = 3_250_000


def test_a_price_nobody_has_published_against_is_posted_unflagged(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    """No index, no judgement.

    An empty table is the normal state of a region nobody has traded in yet, and treating
    that as suspicious would flag every listing from every new seller on the platform.
    """
    headers = signed_in()
    body = listing_body(unit_price_santim=AVERAGE_SANTIM * 50)

    response = client.post("/v1/listings", headers=headers, json=body)

    assert response.status_code == 201, response.text
    assert response.json()["price_outlier"] is False
    assert stored_listings()[0]["price_outlier"] is False


def test_an_index_that_says_it_knew_too_little_leaves_the_price_alone(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    publish_price_index: Callable[..., None],
) -> None:
    """A row that declines to judge is not a judgement that the price is wrong.

    Two sales last month is not an average, and a check that treated it as one would flag
    sellers in exactly the regions we have the least data about.
    """
    headers = signed_in()
    publish_price_index(average_santim=AVERAGE_SANTIM, sample_size=2, insufficient_data=True)

    response = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=AVERAGE_SANTIM * 9)
    )

    assert response.status_code == 201, response.text
    assert response.json()["price_outlier"] is False


def test_a_price_far_outside_the_published_average_is_flagged_and_still_posted(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    publish_price_index: Callable[..., None],
) -> None:
    """Nine times the average is worth saying, and is not worth refusing.

    The listing exists afterwards, because the seller knows their crop and the average does
    not. A flag the caller can see and the buyer can read is the honest outcome; a refusal
    would hide the listing and the seller would only learn that from a buyer who went
    elsewhere.
    """
    headers = signed_in()
    publish_price_index(average_santim=AVERAGE_SANTIM)

    response = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=AVERAGE_SANTIM * 9)
    )

    assert response.status_code == 201, response.text
    assert response.json()["price_outlier"] is True


def test_a_price_inside_the_published_average_is_not_flagged(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    publish_price_index: Callable[..., None],
) -> None:
    """The ordinary case: the asking price matches the market and nobody is warned."""
    headers = signed_in()
    publish_price_index(average_santim=AVERAGE_SANTIM)

    response = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=AVERAGE_SANTIM * 2)
    )

    assert response.status_code == 201, response.text
    assert response.json()["price_outlier"] is False


def test_the_average_is_looked_up_for_the_crop_and_region_it_belongs_to(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    publish_price_index: Callable[..., None],
) -> None:
    """Wheat in Afar says nothing about teff in Oromia.

    A price index that matched on the region alone would flag every listing in the country
    from one region being unusual.
    """
    headers = signed_in()
    publish_price_index(crop_id=2, region_code="AA", average_santim=10)

    response = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=AVERAGE_SANTIM * 20)
    )

    assert response.status_code == 201, response.text
    assert response.json()["price_outlier"] is False


def test_the_latest_published_day_is_the_one_a_price_is_judged_against(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    publish_price_index: Callable[..., None],
) -> None:
    """Yesterday's average is used when this morning's has not run yet.

    Refusing to judge because a job was late would make the rule different depending on when
    a seller happened to post, which is not a rule anybody can rely on.
    """
    headers = signed_in()
    publish_price_index(average_santim=100_000, day=date(2020, 1, 1))
    publish_price_index(average_santim=AVERAGE_SANTIM, day=date(2026, 1, 1))

    response = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=AVERAGE_SANTIM * 8)
    )

    assert response.status_code == 201, response.text
    assert response.json()["price_outlier"] is True


def test_an_index_published_for_today_does_not_judge_a_price_still_in_the_future(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    publish_price_index: Callable[..., None],
) -> None:
    """An average from a day that has not happened cannot be compared with today's price."""
    headers = signed_in()
    publish_price_index(average_santim=1, day=date(2030, 1, 1))

    response = client.post("/v1/listings", headers=headers, json=listing_body())

    assert response.status_code == 201, response.text
    assert response.json()["price_outlier"] is False


def test_an_index_row_with_no_average_declines_to_judge(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    publish_price_index: Callable[..., None],
) -> None:
    """A row can say there is no average without saying there is a zero one.

    Zero would be a price, and every listing against it would look like a mistake.
    """
    headers = signed_in()
    publish_price_index(average_santim=None, insufficient_data=False)

    response = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=AVERAGE_SANTIM * 20)
    )

    assert response.status_code == 201, response.text
    assert response.json()["price_outlier"] is False


def test_the_flag_survives_into_the_read_a_buyer_makes(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    publish_price_index: Callable[..., None],
) -> None:
    """A flag nobody downstream can read is a flag nobody is told about."""
    headers = signed_in()
    publish_price_index(average_santim=AVERAGE_SANTIM)
    created = client.post(
        "/v1/listings", headers=headers, json=listing_body(unit_price_santim=AVERAGE_SANTIM * 7)
    ).json()

    read = client.get(f"/v1/listings/{created['id']}", headers=headers)

    assert read.status_code == 200, read.text
    assert read.json()["price_outlier"] is True
