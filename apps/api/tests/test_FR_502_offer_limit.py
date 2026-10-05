"""The cap on how many offers a listing may carry at once.

Ten open offers is a fairness rule rather than an inventory one: an open offer reserves nothing,
so ten buyers bidding for the same hundred kilos are all bidding honestly, and the eleventh is
noise that would bury the ten. The interesting part is that "open" is judged against the clock
rather than off the stored status, so a bid nobody answered in time stops occupying a slot by
itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi.testclient import TestClient

# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

#: Ten, spelled out rather than imported, so that a change to the limit is a change to this
#: test and not a change to the test's agreement with itself.
MAX_OPEN = 10

#: The traders these tests fill a listing with. A separate number block from the fixtures' own
#: defaults, because a buyer who is secretly the seller is refused as a self-offer and the cap
#: would then be measured one short.
BUYER_BLOCK = "+251912"

#: The trader who turns up to bid eleventh, used by every test that needs one more buyer than
#: the cap allows and must not be one of the ten.
EXTRA_BUYER = f"{BUYER_BLOCK}009009"

#: That buyer's address, distinct from the ten above it so the per-minute sign-in ceiling does
#: not turn a test about the offer cap into a test about the auth guard.
EXTRA_ADDRESS = "198.51.100.99"


def post_listing(
    client: TestClient, headers: dict[str, str], body: Callable[..., dict[str, object]]
) -> str:
    """Post a listing and hand back its identity."""
    response = client.post("/v1/listings", headers=headers, json=body())
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def distinct_buyers(
    signed_in: Callable[..., dict[str, str]], count: int, start: int = 0
) -> list[dict[str, str]]:
    """Sign in a run of different traders, so each bid comes from somebody new.

    One account bidding ten times would be one buyer flooding, which the cap does not exist to
    stop; the rule is about how many buyers a seller has to choose between. Each trader signs in
    from their own address, because eleven traders on one network is a morning in a town and
    eleven sign-ins from one address is a script.
    """
    return [
        signed_in(f"{BUYER_BLOCK}{start + index:06d}", from_address=f"198.51.100.{index + 1}")
        for index in range(count)
    ]


def test_ten_buyers_may_bid_and_the_eleventh_may_not(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """The tenth is served and the eleventh is refused, with the count in the refusal.

    Refused as a 422 rather than a 409: the offer was a well-formed request that the listing's
    own state will not carry, which is the same kind of answer as a bid above the ceiling.
    """
    listing_id = post_listing(client, signed_in(), listing_body)

    for buyer in distinct_buyers(signed_in, MAX_OPEN):
        response = client.post(
            f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body()
        )
        assert response.status_code == 201, response.text

    eleventh = client.post(
        f"/v1/listings/{listing_id}/offers",
        headers=signed_in(EXTRA_BUYER, from_address=EXTRA_ADDRESS),
        json=offer_body(),
    )

    assert eleventh.status_code == 422, eleventh.text
    body = eleventh.json()["error"]
    assert body["code"] == "OFFER_LIMIT"
    assert body["details"]["limit"] == MAX_OPEN
    assert body["details"]["open_offers"] == MAX_OPEN
    assert len(stored_offers()) == MAX_OPEN, "the refused offer must not have been written"


def test_the_cap_is_per_listing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """A full listing does not exhaust a seller's other listings.

    The cap is a fairness rule about how many bids one seller can read on one crop, so it counts
    within a listing. Applied globally it would let one busy listing close negotiation on every
    other crop a seller has.
    """
    seller = signed_in()
    first_listing = post_listing(client, seller, listing_body)
    second_listing = post_listing(client, seller, listing_body)
    buyers = distinct_buyers(signed_in, MAX_OPEN)
    for buyer in buyers:
        assert (
            client.post(
                f"/v1/listings/{first_listing}/offers", headers=buyer, json=offer_body()
            ).status_code
            == 201
        )

    full = client.post(
        f"/v1/listings/{first_listing}/offers",
        headers=signed_in(EXTRA_BUYER, from_address=EXTRA_ADDRESS),
        json=offer_body(),
    )
    assert full.status_code == 422, full.text

    elsewhere = client.post(
        f"/v1/listings/{second_listing}/offers",
        headers=signed_in(EXTRA_BUYER, from_address=EXTRA_ADDRESS),
        json=offer_body(),
    )

    assert elsewhere.status_code == 201, elsewhere.text


def test_an_answered_offer_stops_counting_against_the_cap(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """Superseded and refused offers are history, not occupancy.

    Once a seller has answered the first bid, that row is a record of what was offered and
    should not keep one of the ten places warm.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyers = distinct_buyers(signed_in, MAX_OPEN)
    for buyer in buyers:
        client.post(f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body())

    first = client.get(f"/v1/listings/{listing_id}/offers", headers=seller).json()[0]
    answered = client.post(f"/v1/offers/{first['id']}/reject", headers=seller)
    assert answered.status_code == 200, answered.text

    again = client.post(
        f"/v1/listings/{listing_id}/offers",
        headers=signed_in(EXTRA_BUYER, from_address=EXTRA_ADDRESS),
        json=offer_body(),
    )

    assert again.status_code == 201, again.text


def test_an_expired_offer_stops_counting_against_the_cap(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
    lapse_offer: Callable[[str], None],
) -> None:
    """A bid whose deadline has passed gives its slot back without anything having run.

    This is the rule most worth pinning down. Counting off the stored `OPEN` would mean a
    listing where every buyer had timed out refused new bids, and the reason for it would be
    invisible to the buyer whose bid they were waiting on.
    """
    listing_id = post_listing(client, signed_in(), listing_body)
    buyers = distinct_buyers(signed_in, MAX_OPEN)
    placed = []
    for buyer in buyers:
        response = client.post(
            f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body()
        )
        assert response.status_code == 201, response.text
        placed.append(response.json())
    assert len(placed) == MAX_OPEN

    lapsed = placed[0]
    lapse_offer(str(lapsed["id"]))

    fresh = client.post(
        f"/v1/listings/{listing_id}/offers",
        headers=signed_in(EXTRA_BUYER, from_address=EXTRA_ADDRESS),
        json=offer_body(),
    )

    assert fresh.status_code == 201, fresh.text
    stored = stored_offers()
    assert len(stored) == MAX_OPEN + 1
    assert all(
        row["status"] == "OPEN" for row in stored
    ), "no job has run, so nothing has written EXPIRED: the deadline was read, not recorded"

    lapsed_row = next(row for row in stored if row["id"] == uuid.UUID(str(lapsed["id"])))
    deadline = cast("datetime", lapsed_row["valid_until"])
    assert deadline < datetime.now(UTC), "and yet that row's deadline is in the past"
