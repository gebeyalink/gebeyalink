"""Placing an offer, and the shapes that are not offers.

An offer is a buyer's terms against a live listing: a weight, a price, and how long the bid
should stand. Everything else about it is decided by the platform rather than asked for, because
an offer a client could name its own status or thread in would be a way to write into somebody
else's negotiation.

What is checked here is the arithmetic the listing imposes - the floor, the ceiling, the window -
that a lapsed or paused listing takes nothing, and that a seller cannot bid on their own crop.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient

# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

#: The default listing asks 3,250,000 santim a quintal-equivalent, so twice the ask is this.
#: Named because the ceiling assertions below are about the rule, not about multiplication.
UNIT_PRICE_SANTIM = 3_250_000
CEILING_SANTIM = UNIT_PRICE_SANTIM * 2


def post_listing(
    client: TestClient, headers: dict[str, str], body: Callable[..., dict[str, object]]
) -> str:
    """Post a listing and hand back its identity."""
    response = client.post("/v1/listings", headers=headers, json=body())
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def place_offer(
    client: TestClient,
    headers: dict[str, str],
    listing_id: str,
    body: Callable[..., dict[str, object]],
    **overrides: object,
) -> httpx.Response:
    """Place an offer and hand back the response, status code and body alike."""
    return cast(
        "httpx.Response",
        client.post(f"/v1/listings/{listing_id}/offers", headers=headers, json=body(**overrides)),
    )


def test_an_offer_is_placed_and_reads_back_as_the_buyer_stated_it(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """The terms the buyer sent are the terms stored, with the deadline worked out for them.

    The buyer sends hours because hours is what a person means by "give me a day to think about
    it"; the platform stores the instant that is, because the deadline has to stay the one that
    was true when the offer was made.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")

    before = datetime.now(UTC)
    response = place_offer(
        client, buyer, listing_id, offer_body, quantity_kg=750, price_santim=3_400_000
    )

    assert response.status_code == 201, response.text
    placed = response.json()
    assert placed["listing_id"] == listing_id
    assert placed["quantity_kg"] == 750
    assert placed["price_santim"] == 3_400_000
    assert placed["status"] == "OPEN"
    assert placed["thread_depth"] == 0
    assert placed["proposed_by"] == "BUYER"
    assert placed["version"] == 1
    expected = before + timedelta(hours=72)
    assert (
        abs((datetime.fromisoformat(placed["valid_until"]) - expected).total_seconds()) < 120
    ), "the deadline is the moment the window the buyer asked for ends"


def test_a_new_offer_opens_its_own_thread(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """The thread is the bid's own, and the database agrees.

    Two buyers on one listing must not find themselves answering each other, so the thread is
    written rather than derived from the listing.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)

    first = place_offer(client, signed_in("+251911000003"), listing_id, offer_body)
    second = place_offer(client, signed_in("+251911000004"), listing_id, offer_body)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["thread_id"] != second.json()["thread_id"]

    stored = stored_offers()
    assert len(stored) == 2
    assert stored[0]["thread_depth"] == 0
    assert stored[0]["status"] == "OPEN"


@pytest.mark.parametrize(
    "overrides",
    [
        {"price_santim": CEILING_SANTIM},
        {"price_santim": UNIT_PRICE_SANTIM},
        {"quantity_kg": 100},
        {"quantity_kg": 2_000},
        {"valid_hours": 24},
        {"valid_hours": 168},
    ],
)
def test_the_edges_of_the_rules_are_inside_them(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    overrides: dict[str, object],
) -> None:
    """A bid of exactly twice the ask is allowed, and one quintal-equivalent is allowed.

    Both ends of every bound are inclusive. This is where a strict comparison would show up: the
    request would be refused while looking as though it had been served.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)

    response = place_offer(client, signed_in("+251911000003"), listing_id, offer_body, **overrides)

    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"price_santim": CEILING_SANTIM + 1}, "price_santim"),
        ({"price_santim": 0}, "price_santim"),
        ({"price_santim": -1}, "price_santim"),
        ({"quantity_kg": 99}, "quantity_kg"),
        ({"quantity_kg": 2_001}, "quantity_kg"),
        ({"valid_hours": 23}, "valid_hours"),
        ({"valid_hours": 169}, "valid_hours"),
    ],
)
def test_the_shapes_the_listing_forbids_are_refused_by_field(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    overrides: dict[str, object],
    field: str,
) -> None:
    """Past a bound is a 422 that names the field, not a 400 or a 500.

    A buyer who priced at two and a half times the ask needs to be told which of the three
    numbers was wrong, so that the app can put a marker under the right input.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)

    response = place_offer(client, signed_in("+251911000003"), listing_id, offer_body, **overrides)

    assert response.status_code == 422, response.text
    details = response.json()["error"]["details"]
    assert field in details["fields"], details


def test_a_seller_cannot_offer_on_their_own_listing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """Self-offering is a validation error, because there is nobody to negotiate with.

    Refused as a 422 rather than a 403: the caller is allowed to bid in general, and this is a
    shape of request that is wrong rather than a thing they are not allowed to do.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)

    response = place_offer(client, seller, listing_id, offer_body)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "listing_id" in response.json()["error"]["details"]["fields"]


@pytest.mark.parametrize("move", ["pause", "close"])
def test_a_listing_that_is_not_on_sale_takes_no_offer(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    move: str,
) -> None:
    """A paused or closed listing is not bidding on, and says so in its own words.

    `LISTING_NOT_ACTIVE` rather than a generic refusal, so a client can tell the seller-side
    problem apart from a buyer's own bad request.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    assert client.post(f"/v1/listings/{listing_id}/{move}", headers=seller).status_code == 200

    response = place_offer(client, signed_in("+251911000003"), listing_id, offer_body)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "LISTING_NOT_ACTIVE"


def test_a_lapsed_listing_takes_no_offer(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    lapse_listing: Callable[[str], None],
) -> None:
    """Expiry is judged against the clock, not off the stored status.

    Nothing has had to run for this listing to expire, and equally nothing has to have run for
    a bid on it to be refused - which is the same rule listings use to disappear from search.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    lapse_listing(listing_id)

    response = place_offer(client, signed_in("+251911000003"), listing_id, offer_body)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "LISTING_NOT_ACTIVE"
    assert response.json()["error"]["details"]["status"] == "EXPIRED"


def test_an_offer_on_a_listing_that_does_not_exist_is_not_found(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """A buyer learns whether the listing is there, which is what the listing view would say."""
    response = place_offer(client, signed_in(), str(uuid.uuid4()), offer_body)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_placing_an_offer_needs_a_session(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """Only reference data is public, and an offer is not reference data."""
    listing_id = post_listing(client, signed_in(), listing_body)

    response = client.post(f"/v1/listings/{listing_id}/offers", json=offer_body())

    assert response.status_code == 401, response.text


def test_any_verified_trader_may_bid_whatever_their_label(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """Selling does not disqualify a buyer.

    Every account starts as a farmer and there is no journey yet for becoming a buyer, so
    refusing on the label would refuse every real trader on the platform. Who may bid is decided
    by the seller's floor and by nobody's role.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    farmer_buyer = signed_in("+251911000003")

    response = place_offer(client, farmer_buyer, listing_id, offer_body)

    assert response.status_code == 201, response.text


def test_the_seller_may_not_set_an_offers_status_or_thread(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """Fields the negotiation derives are not fields a client may send.

    `status`, `thread_id`, `thread_depth` and `buyer_id` are all decided by the platform. A
    request carrying them is refused rather than quietly having them ignored, because a client
    that set them and got a 201 would believe it had.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    body = offer_body()

    for field, value in (
        ("status", "ACCEPTED"),
        ("buyer_id", str(uuid.uuid4())),
        ("thread_depth", 3),
        ("thread_id", str(uuid.uuid4())),
    ):
        response = client.post(
            f"/v1/listings/{listing_id}/offers",
            headers=buyer,
            json={**body, field: value},
        )
        assert response.status_code == 422, f"{field}: {response.text}"
