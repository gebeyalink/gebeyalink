"""Answering an offer, and the depth at which a negotiation is left behind.

A counter is a new offer, not an edit. The offer that was answered becomes `SUPERSEDED` and a
new row one level deeper takes its place, so a thread stays readable afterwards: what was
offered, what was asked back, and in what order.

Two things decide who may answer. The first is the row: the party who wrote these terms may not
answer them, and which party that was is read off the depth rather than stored. The second is
the listing: the answering party is always either the seller of this crop or the buyer who
opened the thread, so a signed-in stranger has nothing to answer. The chain is three answers
deep, and the fourth is refused rather than accepted and numbered.

Refusing is not answering, and is held to a different rule: it is the seller's at every depth,
including against the seller's own counter, because it is a word about the listing rather than
a move in the thread.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient

# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

BUYER = "+251911000003"
STRANGER = "+251911000004"

#: Three counters, so four rows: the buyer's bid, and three answers to it.
MAX_DEPTH = 3


def post_listing(
    client: TestClient, headers: dict[str, str], body: Callable[..., dict[str, object]]
) -> str:
    """Post a listing and hand back its identity."""
    response = client.post("/v1/listings", headers=headers, json=body())
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def open_bid(
    client: TestClient,
    buyer: dict[str, str],
    listing_id: str,
    offer_body: Callable[..., dict[str, object]],
    **overrides: object,
) -> dict[str, object]:
    """Place the buyer's opening bid, which every thread in this file starts with."""
    response = client.post(
        f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body(**overrides)
    )
    assert response.status_code == 201, response.text
    return cast("dict[str, Any]", response.json())


def counter(
    client: TestClient,
    headers: dict[str, str],
    offer_id: str,
    offer_body: Callable[..., dict[str, object]],
    **overrides: object,
) -> httpx.Response:
    """Answer an offer."""
    return cast(
        "httpx.Response",
        client.post(
            f"/v1/offers/{offer_id}/counter", headers=headers, json=offer_body(**overrides)
        ),
    )


def test_a_counter_supersedes_the_offer_it_answers_and_starts_a_new_row(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """Nothing is edited: the old row stays, marked answered, and a new row stands in its place.

    Both writes land together, so a negotiation is never left with the offer answered and
    nothing in its place - which is the failure a separate write would produce.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    original = open_bid(client, signed_in(BUYER), listing_id, offer_body)

    answered = counter(client, seller, str(original["id"]), offer_body, price_santim=3_500_000)

    assert answered.status_code == 200, answered.text
    answer = answered.json()
    assert answer["id"] != original["id"]
    assert answer["thread_id"] == original["thread_id"]
    assert answer["thread_depth"] == 1
    assert answer["price_santim"] == 3_500_000
    assert answer["status"] == "OPEN"
    assert answer["proposed_by"] == "SELLER"

    stored = stored_offers()
    assert len(stored) == 2
    assert stored[0]["status"] == "SUPERSEDED"
    assert stored[0]["thread_depth"] == 0
    assert stored[1]["status"] == "OPEN"
    assert stored[1]["thread_depth"] == 1


def test_a_counter_keeps_the_same_buyer_however_deep_it_goes(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """The buyer is who the negotiation is with, and the seller's answer does not change that.

    A counter is the seller's *terms*, so the depth says who is speaking; the buyer column says
    whose crop is being bought. Conflating the two would let a seller's answer look like a bid
    from a different buyer to anything reading the row.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    original = open_bid(client, buyer, listing_id, offer_body)

    answer = counter(client, seller, str(original["id"]), offer_body).json()
    buyer_answer = counter(client, buyer, str(answer["id"]), offer_body).json()

    assert buyer_answer["buyer_id"] == original["buyer_id"]
    assert [str(row["buyer_id"]) for row in stored_offers()] == [
        original["buyer_id"],
        original["buyer_id"],
        original["buyer_id"],
    ]


def test_the_two_sides_alternate_without_either_naming_their_turn(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """The seller answers the buyer, then the buyer answers the seller.

    Neither of them has to know whose turn it is: the depth of the offer they are holding says
    it, which is why a client cannot offer at the wrong moment and have it work.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    original = open_bid(client, buyer, listing_id, offer_body)

    depths = [(original["thread_depth"], original["proposed_by"])]
    current = str(original["id"])
    for index, answerer in enumerate((seller, buyer, seller), start=1):
        answered = counter(
            client, answerer, current, offer_body, price_santim=3_400_000 + index * 10_000
        )
        assert answered.status_code == 200, answered.text
        body = answered.json()
        depths.append((body["thread_depth"], body["proposed_by"]))
        current = str(body["id"])

    assert depths == [
        (0, "BUYER"),
        (1, "SELLER"),
        (2, "BUYER"),
        (3, "SELLER"),
    ]


def test_a_fourth_answer_is_refused_and_the_thread_stays_three_deep(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """Three answers is the limit, and the fourth is a 422 that says so.

    Bounded because a negotiation nobody is closing is a negotiation nobody is having. The limit
    is on the answer rather than on time, so a fast exchange cannot run further than a slow one.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    current = str(open_bid(client, buyer, listing_id, offer_body)["id"])

    for index, answerer in enumerate((seller, buyer, seller), start=1):
        answered = counter(client, answerer, current, offer_body, price_santim=3_400_000 + index)
        assert answered.status_code == 200, answered.text
        current = str(answered.json()["id"])

    too_deep = counter(client, buyer, current, offer_body)

    assert too_deep.status_code == 422, too_deep.text
    details = too_deep.json()["error"]["details"]
    assert too_deep.json()["error"]["code"] == "LIMIT_EXCEEDED"
    assert details["thread_depth"] == MAX_DEPTH
    assert details["limit"] == MAX_DEPTH
    assert len(stored_offers()) == MAX_DEPTH + 1, "the refused answer was not written"


def test_the_party_that_made_the_terms_cannot_answer_them(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """A buyer cannot bargain with themselves, and a seller cannot accept by asking again.

    Allowing either would let one row be moved without the counterparty, which is the one thing
    a negotiation is not. Refused as a 403: the caller is a valid trader, and this particular
    row is not theirs to move.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    original = open_bid(client, buyer, listing_id, offer_body)

    by_buyer = counter(client, buyer, str(original["id"]), offer_body)
    assert by_buyer.status_code == 403, by_buyer.text
    assert by_buyer.json()["error"]["code"] == "FORBIDDEN"
    assert by_buyer.json()["error"]["details"]["proposed_by"] == "BUYER"

    answered = counter(client, seller, str(original["id"]), offer_body)
    assert answered.status_code == 200, answered.text
    by_seller = counter(client, seller, str(answered.json()["id"]), offer_body)
    assert by_seller.status_code == 403, by_seller.text

    assert len(stored_offers()) == 2, "neither refused answer was written"


def test_a_stranger_may_not_answer_anybody_s_offer(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """Being signed in is not standing in a negotiation.

    The answering party is either this listing's seller or the buyer who opened the thread, and a
    third trader is neither. SEC-05: no path here reaches somebody else's row.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    original = open_bid(client, signed_in(BUYER), listing_id, offer_body)

    refused = counter(client, signed_in(STRANGER), str(original["id"]), offer_body)

    assert refused.status_code == 403, refused.text
    assert len(stored_offers()) == 1


def test_a_counter_is_held_to_the_same_bounds_as_the_bid_it_answers(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """An answer cannot propose something an opening bid could not have proposed.

    Otherwise the ceiling and the floor would be worth nothing: a seller who wanted to buy at
    four times the ask would simply bid instead of counter-bidding.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    original = open_bid(client, signed_in(BUYER), listing_id, offer_body)

    too_high = counter(client, seller, str(original["id"]), offer_body, price_santim=6_500_001)
    too_small = counter(client, seller, str(original["id"]), offer_body, quantity_kg=99)

    assert too_high.status_code == 422, too_high.text
    assert "price_santim" in too_high.json()["error"]["details"]["fields"]
    assert too_small.status_code == 422, too_small.text
    assert "quantity_kg" in too_small.json()["error"]["details"]["fields"]
    assert (
        stored_offers()[0]["status"] == "OPEN"
    ), "the offer answered by a refused counter is still on the table"


def test_the_buyer_may_not_refuse_the_sellers_counter(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """A refusal is the seller's, and the depth must not hand it to the buyer.

    At an odd depth the seller's own terms are the ones on the table, so parity says the buyer
    may answer them. Answering is not refusing: had rejection followed the same rule, a buyer
    could end the negotiation with the seller's "no" recorded under their name, and a seller
    whose counter came back as rejected would have no answer of their own to show for it. The
    refusal is therefore held to the listing's seller at every depth.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    original = open_bid(client, buyer, listing_id, offer_body)
    answered = counter(client, seller, str(original["id"]), offer_body)
    assert answered.status_code == 200, answered.text
    seller_counter = str(answered.json()["id"])
    assert answered.json()["thread_depth"] == 1

    refused = client.post(f"/v1/offers/{seller_counter}/reject", headers=buyer)

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "FORBIDDEN"
    assert refused.json()["error"]["details"]["reason"] == "not_the_seller"
    answered_rows = [offer for offer in stored_offers() if offer["thread_depth"] == 1]
    assert len(answered_rows) == 1, "the refused rejection wrote a row of its own"
    assert answered_rows[0]["status"] == "OPEN", "the counter was refused after all"


def test_the_seller_may_refuse_their_own_counter(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """And the seller holds the refusal at an odd depth too.

    The right above refuses the buyer, not the counter: refusing is the listing owner's word on
    any bid for that listing, so the seller can withdraw their own terms from the table.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    original = open_bid(client, signed_in(BUYER), listing_id, offer_body)
    answered = counter(client, seller, str(original["id"]), offer_body)
    assert answered.status_code == 200, answered.text

    refused = client.post(f"/v1/offers/{answered.json()['id']}/reject", headers=seller)

    assert refused.status_code == 200, refused.text
    assert refused.json()["status"] == "REJECTED"


def test_a_seller_cannot_answer_on_a_listing_that_is_no_longer_on_sale(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """Answering proposes new terms, so it needs a listing that still takes them.

    Refusing and withdrawing do not, which is why a seller can still say no on a paused listing
    but cannot put a fresh question to a buyer about it.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    original = open_bid(client, buyer, listing_id, offer_body)
    assert client.post(f"/v1/listings/{listing_id}/pause", headers=seller).status_code == 200

    refused = counter(client, seller, str(original["id"]), offer_body)

    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "LISTING_NOT_ACTIVE"
    assert stored_offers()[0]["status"] == "OPEN"


def test_a_seller_can_still_refuse_on_a_paused_listing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """The asymmetry with the counter above is deliberate, and both halves are here.

    Refusing proposes nothing and reserves nothing, so there is no reason a seller should be
    unable to close out a bid just because the listing behind it paused. A buyer would otherwise
    be left holding an open offer on a crop nobody is selling.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    original = open_bid(client, buyer, listing_id, offer_body)
    assert client.post(f"/v1/listings/{listing_id}/pause", headers=seller).status_code == 200

    refused = client.post(f"/v1/offers/{original['id']}/reject", headers=seller)

    assert refused.status_code == 200, refused.text
    assert refused.json()["status"] == "REJECTED"


def test_the_seller_sees_the_whole_thread_in_the_order_it_happened(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """Oldest first, so the list reads as a conversation rather than a pile.

    A negotiation whose earlier terms are missing is not something a seller can act on, so this
    is one list rather than a page that would have to be read backwards.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    first = open_bid(client, buyer, listing_id, offer_body)
    second = counter(client, seller, str(first["id"]), offer_body).json()
    third = counter(client, buyer, str(second["id"]), offer_body).json()

    listed = client.get(f"/v1/listings/{listing_id}/offers", headers=seller)

    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert [row["id"] for row in rows] == [first["id"], second["id"], third["id"]]
    assert [row["thread_depth"] for row in rows] == [0, 1, 2]
    assert [row["status"] for row in rows] == ["SUPERSEDED", "SUPERSEDED", "OPEN"]


def test_another_sellers_bid_cannot_be_answered_or_read(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """Two sellers' crops side by side, and neither may touch the other's negotiation.

    Counter, reject and list are all refused for the second seller, which is SEC-05 read from
    the other side: the seller of a *different* listing is as much a stranger to this offer as
    anybody else. The second seller has a listing of their own in this test precisely so that
    the refusals are about the offer rather than about having nothing to sell.
    """
    first_seller = signed_in()
    second_seller = signed_in(STRANGER)
    listing_id = post_listing(client, first_seller, listing_body)
    assert post_listing(client, second_seller, listing_body)
    original = open_bid(client, signed_in(BUYER), listing_id, offer_body)

    refused = [
        counter(client, second_seller, str(original["id"]), offer_body),
        client.post(f"/v1/offers/{original['id']}/reject", headers=second_seller),
        client.get(f"/v1/listings/{listing_id}/offers", headers=second_seller),
    ]

    for response in refused:
        assert response.status_code == 403, response.text
    assert len(stored_offers()) == 1


def test_an_offer_that_does_not_exist_is_not_found(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """Not-found rather than forbidden, so a caller can tell an absent row from a refused one.

    The listing detail view will say whether an offer is there, so answering that question is
    not a leak.
    """
    import uuid

    response = counter(client, signed_in(), str(uuid.uuid4()), offer_body)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_counting_needs_a_session(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """Only reference data is public, and a negotiation is not reference data."""
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    original = open_bid(client, signed_in(BUYER), listing_id, offer_body)

    response = client.post(f"/v1/offers/{original['id']}/counter", json=offer_body())

    assert response.status_code == 401, response.text
