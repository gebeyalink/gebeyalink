"""An offer nobody got to, and the state it is in once its window closes.

The window is the only thing on this platform that is allowed to move a row on its own, and it
does it without a job: an open offer whose deadline has passed reads as expired the moment it is
looked at, and anything trying to move it is refused on the same reading. Nothing has to have
run for either to be true, which is what makes it true at three in the morning on the day after.

The other half of this file is that a lapsed offer is *finished*. It is not reopened by a
counter arriving late, and it does not become the seller's to refuse - which is the difference
between an expired offer and a paused one.
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


def place_and_lapse(
    client: TestClient,
    buyer: dict[str, str],
    listing_id: str,
    offer_body: Callable[..., dict[str, object]],
    lapse_offer: Callable[[str], None],
) -> dict[str, Any]:
    """Place one offer and move its deadline into the past.

    Asking for a window of zero hours is not the way to arrange this: the request rules refuse
    that on purpose, so the deadline has to be moved the way time moves it.
    """
    placed = client.post(f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body())
    assert placed.status_code == 201, placed.text
    body = cast("dict[str, Any]", placed.json())
    lapse_offer(str(body["id"]))
    return body


def test_an_offer_past_its_deadline_reads_as_expired(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
    lapse_offer: Callable[[str], None],
) -> None:
    """The read is the expiry.

    The database still says `OPEN`, because nothing has written and nothing needed to. What the
    caller is told is `EXPIRED`, so a client that renders a bid as still live is wrong about
    something the platform already considered settled.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    placed = place_and_lapse(client, buyer, listing_id, offer_body, lapse_offer)

    listed = client.get(f"/v1/listings/{listing_id}/offers", headers=seller)

    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert rows[0]["id"] == placed["id"]
    assert rows[0]["status"] == "EXPIRED"
    assert (
        stored_offers()[0]["status"] == "OPEN"
    ), "the row is untouched on disk, which is the whole design"


def test_an_expired_offer_cannot_be_countered(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
    lapse_offer: Callable[[str], None],
) -> None:
    """Answering a lapsed bid would be negotiating with somebody who has walked away.

    Refused as a conflict rather than a validation error, because nothing about the *request* is
    wrong - the terms are fine, the offer is simply over.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    placed = place_and_lapse(client, signed_in(BUYER), listing_id, offer_body, lapse_offer)

    response = client.post(
        f"/v1/offers/{placed['id']}/counter",
        headers=seller,
        json=offer_body(price_santim=3_450_000),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ILLEGAL_STATE_TRANSITION"
    assert response.json()["error"]["details"]["status"] == "EXPIRED"
    assert len(stored_offers()) == 1, "nothing may be written onto a finished negotiation"


def test_an_expired_offer_cannot_be_rejected_either(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
    lapse_offer: Callable[[str], None],
) -> None:
    """The refusal does not resurrect a negotiation that has already timed out.

    It reads as a small thing to allow - the seller is only saying no - but a rejection written
    onto an expired offer is a second end to a thread that already ended, and the buyer's history
    would then show two endings.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    placed = place_and_lapse(client, signed_in(BUYER), listing_id, offer_body, lapse_offer)

    response = client.post(f"/v1/offers/{placed['id']}/reject", headers=seller)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["details"]["status"] == "EXPIRED"
    assert stored_offers()[0]["status"] == "OPEN", "a refused move writes nothing"


def test_an_expired_offer_cannot_be_withdrawn(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    lapse_offer: Callable[[str], None],
) -> None:
    """Even the buyer cannot take back a bid that has already ended.

    The buyer's own action, and the rule is the same as for everybody else: once the window has
    closed the row records how the negotiation ended, and that is not the buyer's to change.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    placed = place_and_lapse(client, buyer, listing_id, offer_body, lapse_offer)

    response = client.post(f"/v1/offers/{placed['id']}/withdraw", headers=buyer)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ILLEGAL_STATE_TRANSITION"


def test_an_expired_offer_still_occupies_its_row_in_the_thread(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    lapse_offer: Callable[[str], None],
) -> None:
    """It stays in the seller's list, as a record that it happened and lapsed.

    A seller looking at a listing tomorrow should still see the bid they did not answer, rather
    than a gap where one used to be - the whole point of the history being kept whole.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    placed = place_and_lapse(client, signed_in(BUYER), listing_id, offer_body, lapse_offer)

    listed = client.get(f"/v1/listings/{listing_id}/offers", headers=seller)

    assert [row["id"] for row in listed.json()] == [placed["id"]]
    assert listed.json()[0]["thread_depth"] == 0


def test_an_offer_with_time_left_is_still_open(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """The other side of the rule, so that the expiry test above is not vacuous.

    Without this, an implementation that expired everything immediately would pass every other
    test in this file.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)

    placed = client.post(f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body())
    assert placed.status_code == 201, placed.text

    listed = client.get(f"/v1/listings/{listing_id}/offers", headers=seller)

    assert listed.json()[0]["status"] == "OPEN"
    assert (
        client.post(f"/v1/offers/{placed.json()['id']}/reject", headers=seller).status_code == 200
    )


def test_a_counter_does_not_revive_a_thread_whose_latest_offer_lapsed(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
    lapse_offer: Callable[[str], None],
) -> None:
    """It is the offer being answered that has to be live, not merely some row in the thread.

    A thread where the buyer's bid was answered and then the seller's answer lapsed is over, and
    the buyer cannot go back to the superseded row to reopen it: that row has no standing, and
    allowing it would make a thread infinitely re-enterable from its own history.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in(BUYER)
    original = client.post(
        f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body()
    ).json()

    counter = client.post(f"/v1/offers/{original['id']}/counter", headers=seller, json=offer_body())
    assert counter.status_code == 200, counter.text
    lapse_offer(str(counter.json()["id"]))

    reopened = client.post(
        f"/v1/offers/{original['id']}/counter", headers=seller, json=offer_body()
    )

    assert reopened.status_code == 409, reopened.text
    assert len(stored_offers()) == 2, "a thread that is over must not grow"


def test_the_seller_may_not_read_another_sellers_offers(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """The list is the seller's, and one seller's prices are not another's to read.

    Two traders bidding on one listing would otherwise learn each other's floor from the list,
    which is the market's information leaking between competitors.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    client.post(f"/v1/listings/{listing_id}/offers", headers=signed_in(BUYER), json=offer_body())

    refused = client.get(f"/v1/listings/{listing_id}/offers", headers=signed_in(STRANGER))

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "FORBIDDEN"


def test_a_buyer_may_not_read_the_others_bids_either(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """Knowing what you bid is yours; knowing what the others bid is not.

    The buyer cannot read their own offer back through this list, which is deliberate: the list
    is the seller's view of the market. Their own bid is the one they placed.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    client.post(f"/v1/listings/{listing_id}/offers", headers=signed_in(BUYER), json=offer_body())

    refused = client.get(f"/v1/listings/{listing_id}/offers", headers=signed_in(BUYER))

    assert refused.status_code == 403, refused.text
