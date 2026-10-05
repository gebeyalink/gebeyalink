"""The buyer taking their bid back, and why only the buyer may.

Withdrawal is the buyer withdrawing, which is not the same act as the seller refusing and must
not be reachable by the seller. If a seller could do it, a refusal would read to the buyer as
themselves changing their mind - the buyer would see their own bid marked withdrawn, having
never asked for that.

Both halves of that are checked here: the buyer can, at any depth of a thread, and the seller
cannot at all. The buyer can also only withdraw an offer that is still open, because a
withdrawal is an answer of its own and an answer to a finished negotiation is not on offer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

BUYER = "+251911000003"
STRANGER = "+251911000004"


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
) -> dict[str, Any]:
    """Place the buyer's opening bid."""
    response = client.post(
        f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body(**overrides)
    )
    assert response.status_code == 201, response.text
    return cast("dict[str, Any]", response.json())


def test_the_buyer_withdraws_their_own_open_offer(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """A bid nobody answered can be taken back, and the row says so.

    The terms and the thread are kept. What the buyer wants to stop is their bid being on the
    table; what the seller wants to know is what it was.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    original = open_bid(client, buyer, listing_id, offer_body)

    withdrawn = client.post(f"/v1/offers/{original['id']}/withdraw", headers=buyer)

    assert withdrawn.status_code == 200, withdrawn.text
    body = withdrawn.json()
    assert body["status"] == "WITHDRAWN"
    assert body["id"] == original["id"]
    assert body["quantity_kg"] == original["quantity_kg"]
    assert stored_offers()[0]["status"] == "WITHDRAWN"


def test_a_withdrawn_offer_frees_a_place_against_the_ten_offer_cap(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """Withdrawing is how a buyer says no to their own bid, so it has to give the slot back.

    Without this, a buyer who bid on impulse would leave the seller's tenth place warm for the
    rest of the listing's life, and the cap would be measuring attempts rather than bids.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    original = open_bid(client, buyer, listing_id, offer_body)

    assert client.post(f"/v1/offers/{original['id']}/withdraw", headers=buyer).status_code == 200

    listed = client.get(f"/v1/listings/{listing_id}/offers", headers=seller)
    assert [row["status"] for row in listed.json()] == ["WITHDRAWN"]
    assert len(stored_offers()) == 1


def test_the_seller_withdrawing_a_bid_is_refused(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """A seller who wants a bid to stop refuses it instead.

    SEC-05: withdrawal is the buyer's alone. If the seller could do it, a refusal would be
    reported to the buyer as their own withdrawal, and the two sides' records of the same
    negotiation would disagree about who ended it.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    original = open_bid(client, buyer, listing_id, offer_body)

    refused = client.post(f"/v1/offers/{original['id']}/withdraw", headers=seller)

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "FORBIDDEN"
    assert refused.json()["error"]["details"]["reason"] == "not_the_buyer"
    assert stored_offers()[0]["status"] == "OPEN", "a refused withdrawal writes nothing"


def test_another_buyer_cannot_withdraw_a_bid_that_is_not_theirs(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """The right verb on somebody else's row is still the wrong verb.

    Being able to bid on the same listing does not make a trader a party to another buyer's
    negotiation.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    original = open_bid(client, signed_in(BUYER), listing_id, offer_body)

    refused = client.post(f"/v1/offers/{original['id']}/withdraw", headers=signed_in(STRANGER))

    assert refused.status_code == 403, refused.text


def test_the_buyer_may_withdraw_at_the_bottom_of_the_sellers_terms(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """Withdrawing the seller's answer is still the buyer's to do.

    The buyer column names who the negotiation is with, so it holds at every depth. A buyer who
    walks away from a counter is withdrawing their negotiation, not just their own last message.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    original = open_bid(client, buyer, listing_id, offer_body)
    answer = client.post(f"/v1/offers/{original['id']}/counter", headers=seller, json=offer_body())
    assert answer.status_code == 200, answer.text

    withdrawn = client.post(f"/v1/offers/{answer.json()['id']}/withdraw", headers=buyer)

    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "WITHDRAWN"
    assert withdrawn.json()["thread_depth"] == 1
    assert [row["status"] for row in stored_offers()] == ["SUPERSEDED", "WITHDRAWN"]


@pytest.mark.parametrize("closer", ["reject", "counter", "withdraw"])
def test_an_offer_that_is_already_answered_cannot_be_withdrawn(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    closer: str,
) -> None:
    """A finished negotiation is not withdrawn, whichever word the client reaches for.

    Two endpoints that both end an offer must not also both *begin* one, or a buyer could undo a
    refusal by withdrawing it afterwards.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    original = open_bid(client, buyer, listing_id, offer_body)

    if closer == "reject":
        assert client.post(f"/v1/offers/{original['id']}/reject", headers=seller).status_code == 200
    elif closer == "counter":
        assert (
            client.post(
                f"/v1/offers/{original['id']}/counter", headers=seller, json=offer_body()
            ).status_code
            == 200
        )
    else:
        # Already withdrawn, so the second attempt is the one being refused.
        assert (
            client.post(f"/v1/offers/{original['id']}/withdraw", headers=buyer).status_code == 200
        )

    repeated = client.post(f"/v1/offers/{original['id']}/withdraw", headers=buyer)

    assert repeated.status_code == 409, repeated.text
    assert repeated.json()["error"]["code"] == "ILLEGAL_STATE_TRANSITION"
    assert repeated.json()["error"]["details"]["status"] in {
        "REJECTED",
        "SUPERSEDED",
        "WITHDRAWN",
    }


def test_withdrawing_does_not_touch_the_terms_it_records(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """What was offered survives the withdrawal, for the seller's history.

    A row that blanked its numbers on the way out would leave the seller unable to tell a
    withdrawn bid from one that never existed.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    original = open_bid(client, buyer, listing_id, offer_body, price_santim=3_600_000)

    client.post(f"/v1/offers/{original['id']}/withdraw", headers=buyer)

    listed = client.get(f"/v1/listings/{listing_id}/offers", headers=seller)
    row = listed.json()[0]
    assert row["status"] == "WITHDRAWN"
    assert row["price_santim"] == 3_600_000
    assert row["valid_until"] == original["valid_until"]


def test_an_offer_that_does_not_exist_is_not_found(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
) -> None:
    """Not-found rather than forbidden, so a caller can tell an absent row from a refused one."""
    import uuid

    response = client.post(f"/v1/offers/{uuid.uuid4()}/withdraw", headers=signed_in())

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_withdrawing_needs_a_session(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """Only reference data is public, and a negotiation is not reference data."""
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    original = open_bid(client, signed_in(BUYER), listing_id, offer_body)

    response = client.post(f"/v1/offers/{original['id']}/withdraw")

    assert response.status_code == 401, response.text
