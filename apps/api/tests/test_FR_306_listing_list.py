"""A listing that ran out of time, and a seller looking at what they still have.

Expiry is read rather than written, so these checks put the clock past a listing and read it
back. That is the whole design: nobody has to have run anything for a listing to have
expired, and no request to read one may quietly change it.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration


def test_a_listing_read_before_its_time_is_still_live(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """The ordinary case: a listing is live until the moment it says it is not."""
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()

    read = client.get(f"/v1/listings/{created['id']}", headers=headers)

    assert read.status_code == 200, read.text
    assert read.json()["status"] == "ACTIVE"


def test_a_listing_read_after_its_time_reports_itself_expired(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    lapse_listing: Callable[[str], None],
) -> None:
    """The reason is reported as EXPIRED rather than as ACTIVE and a date in the past.

    A client asked to buy a listing that ran out should be told it ran out, and should not
    have to compare dates itself to work that out.
    """
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    lapse_listing(created["id"])

    read = client.get(f"/v1/listings/{created['id']}", headers=headers)

    assert read.status_code == 200, read.text
    assert read.json()["status"] == "EXPIRED"


def test_an_expired_listing_is_still_readable(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    lapse_listing: Callable[[str], None],
) -> None:
    """Readable on purpose.

    Somebody who was sent a link to a listing before it expired still needs the page, because
    the alternative is a broken link in a message somebody cannot unsend.
    """
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    lapse_listing(created["id"])

    read = client.get(f"/v1/listings/{created['id']}", headers=signed_in())

    assert read.status_code == 200, read.text
    assert read.json()["id"] == created["id"]


def test_reading_a_listing_does_not_itself_expire_it(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    lapse_listing: Callable[[str], None],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    """The stored status is what it was; expiry is a reading, not a write.

    A GET that changed the row would make two readers disagree, and would make the stored
    value depend on who had looked at it and when.
    """
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    lapse_listing(created["id"])

    client.get(f"/v1/listings/{created['id']}", headers=headers)
    client.get(f"/v1/listings/{created['id']}", headers=headers)

    assert stored_listings()[0]["status"] == "ACTIVE"


def test_a_seller_who_let_a_listing_lapse_cannot_still_pause_it(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    lapse_listing: Callable[[str], None],
) -> None:
    """Refused, because the listing has already left the market on its own.

    Letting the pause through would leave a listing the seller believes they stopped, in a
    state that says they stopped it, with no record that it stopped by itself.
    """
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    lapse_listing(created["id"])

    refused = client.post(f"/v1/listings/{created['id']}/pause", headers=headers)

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "ILLEGAL_STATE_TRANSITION"
    assert refused.json()["error"]["details"]["status"] == "EXPIRED"


def test_a_closed_listing_does_not_come_back_as_expired(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    lapse_listing: Callable[[str], None],
) -> None:
    """CLOSED is the answer the seller gave, and it is not replaced by one the clock gives.

    Reading a closed listing as expired would tell a seller their listing expired when they
    are the reason it ended, and would let somebody resume it out of EXPIRED.
    """
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    client.post(f"/v1/listings/{created['id']}/close", headers=headers)
    lapse_listing(created["id"])

    read = client.get(f"/v1/listings/{created['id']}", headers=headers)

    assert read.json()["status"] == "CLOSED"


def test_a_seller_sees_their_own_listings_and_only_their_own(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A seller's list is theirs.

    The endpoint answers "what do I have up", so a listing in it has to be one they posted.
    """
    mine = signed_in()
    client.post("/v1/listings", headers=mine, json=listing_body())
    client.post("/v1/listings", headers=mine, json=listing_body())
    client.post("/v1/listings", headers=signed_in_as_buyer(), json=listing_body())

    listed = client.get("/v1/listings/mine", headers=mine)

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert set(body) == {"data", "next_cursor", "has_more"}
    assert len(body["data"]) == 2


def test_a_seller_with_nothing_posted_is_given_an_empty_page(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """An empty list is a page with nothing in it, not a missing resource."""
    listed = client.get("/v1/listings/mine", headers=signed_in())

    assert listed.status_code == 200, listed.text
    assert listed.json() == {"data": [], "next_cursor": None, "has_more": False}


def test_pages_come_back_newest_first_and_never_repeat_a_listing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Walking the pages gives every listing exactly once.

    Ordering by time alone would drop or repeat listings that were posted in the same
    instant, which two sellers behind the same load balancer will do.
    """
    headers = signed_in()
    for _ in range(5):
        client.post("/v1/listings", headers=headers, json=listing_body())

    first = client.get("/v1/listings/mine?limit=2", headers=headers).json()
    second = client.get(
        f"/v1/listings/mine?limit=2&cursor={first['next_cursor']}", headers=headers
    ).json()
    third = client.get(
        f"/v1/listings/mine?limit=2&cursor={second['next_cursor']}", headers=headers
    ).json()

    codes = [listing["short_code"] for page in (first, second, third) for listing in page["data"]]
    assert len(codes) == 5
    assert len(set(codes)) == 5
    assert first["has_more"] is True
    assert third["has_more"] is False
    assert third["next_cursor"] is None


def test_the_first_page_is_the_newest(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A seller looking at their list is looking at the newest thing they did.

    Newest first also matches how the rows are stored, so the first page is the one that
    needs no tie-breaking at all.
    """
    headers = signed_in()
    first = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    client.post("/v1/listings", headers=headers, json=listing_body())

    listed = client.get("/v1/listings/mine", headers=headers).json()
    newest = listed["data"][0]

    assert newest["id"] != first["id"]
    assert first["created_at"] < newest["created_at"]


def test_a_limit_of_more_than_the_whole_list_is_answered_with_the_whole_list(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Asking for a hundred and owning two gives two, not an error and not ninety-eight
    invented ones."""
    headers = signed_in()
    client.post("/v1/listings", headers=headers, json=listing_body())

    listed = client.get("/v1/listings/mine?limit=100", headers=headers)

    assert listed.status_code == 200, listed.text
    assert len(listed.json()["data"]) == 1


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_a_limit_outside_the_stated_range_is_refused(
    client: TestClient, signed_in: Callable[..., dict[str, str]], limit: int
) -> None:
    """Refused rather than clamped.

    A client asking for zero has a bug, and answering it with a default page would hide that
    bug until somebody wondered why their list looked full.
    """
    response = client.get(f"/v1/listings/mine?limit={limit}", headers=signed_in())

    assert response.status_code == 422, response.text


def test_a_cursor_this_server_did_not_write_is_refused(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """A cursor is an opaque token the server issued; anything else is a client bug.

    Guessing at one would mean reconstructing an ordering from something that was never part
    of one.
    """
    response = client.get("/v1/listings/mine?cursor=not-a-cursor", headers=signed_in())

    assert response.status_code == 422, response.text


def test_a_listing_does_not_go_missing_from_a_seller_s_own_list_because_it_expired(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    lapse_listing: Callable[[str], None],
) -> None:
    """Expired or not, the seller still posted it.

    Dropping it from the list would answer the question "what did I post" with "what is
    still for sale", which is a different question and one they also want answered, on
    another screen.
    """
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    lapse_listing(created["id"])

    listed = client.get("/v1/listings/mine", headers=headers).json()

    assert [listing["id"] for listing in listed["data"]] == [created["id"]]
    assert listed["data"][0]["status"] == "EXPIRED"


def test_looking_at_your_own_list_needs_a_signature(
    client: TestClient,
) -> None:
    """Nobody's list is available to everybody, because it is nobody else's business."""
    assert client.get("/v1/listings/mine").status_code == 401


def test_a_listing_whose_stock_is_all_committed_reads_as_sold_out_without_anything_running(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    """FR-306's second trigger: availability reaching zero, from any direction.

    A sale that empties a listing writes `SOLD_OUT` in its own transaction, but the write is not
    the only way a listing can reach zero - and the case here is one a sale cannot produce: an
    edit that cuts the listing down to exactly what its orders already hold. Nobody ran anything,
    the stored column still says `ACTIVE`, and the listing reads as sold out.
    """
    headers = signed_in()
    created = client.post(
        "/v1/listings",
        headers=headers,
        json=listing_body(quantity_kg=800, min_order_kg=100),
    ).json()
    bought = client.post(
        f"/v1/listings/{created['id']}/buy-now",
        headers={**signed_in("+251911000003"), "Idempotency-Key": idempotency_key()},
        json=order_body(quantity_kg=400),
    )
    assert bought.status_code == 201, bought.text
    assert [row["status"] for row in stored_listings()] == ["ACTIVE"]

    edited = client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"version": created["version"], "quantity_kg": 400},
    )

    assert edited.status_code == 200, edited.text
    assert edited.json()["available_kg"] == 0
    assert edited.json()["status"] == "SOLD_OUT"
    listed = client.get("/v1/listings/mine", headers=headers).json()
    assert listed["data"][0]["status"] == "SOLD_OUT"
    # Derived, not written: a status a read wrote would depend on who had looked and when.
    assert [row["status"] for row in stored_listings()] == ["ACTIVE"]


def test_a_paused_listing_with_nothing_left_stays_paused(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The system transition does not overrule the seller's own statement about their listing.

    §10.1 has no edge into `SOLD_OUT` from anywhere but `ACTIVE`, so a listing emptied while it was
    paused reads as paused. It is not for sale, which is what the seller asked for, and the seller
    who resumes it and finds nothing left has been told the truth by the arithmetic rather than by a
    status that changed while they were not looking.
    """
    headers = signed_in()
    created = client.post(
        "/v1/listings",
        headers=headers,
        json=listing_body(quantity_kg=800, min_order_kg=100),
    ).json()
    bought = client.post(
        f"/v1/listings/{created['id']}/buy-now",
        headers={**signed_in("+251911000003"), "Idempotency-Key": idempotency_key()},
        json=order_body(quantity_kg=400),
    )
    assert bought.status_code == 201, bought.text
    paused = client.post(f"/v1/listings/{created['id']}/pause", headers=headers)
    assert paused.status_code == 200, paused.text

    edited = client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"version": paused.json()["version"], "quantity_kg": 400},
    )
    assert edited.status_code == 200, edited.text

    read = client.get(f"/v1/listings/{created['id']}", headers=headers)

    assert read.json()["status"] == "PAUSED"
    assert read.json()["available_kg"] == 0
