"""Pausing, resuming and closing, and the moves that are not on the table.

The three endpoints exist as three names rather than one that takes a status, so a client
never has to know the state table in order to know whether its request will work. What is
checked here is that each name means exactly one state to move from, that the refusals come
back as conflicts rather than as successes that quietly did nothing, and that a listing
belongs to the seller who posted it.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration


def post_listing(
    client: TestClient, headers: dict[str, str], body: Callable[..., dict[str, object]]
) -> str:
    """Post a listing and hand back its identity."""
    response = client.post("/v1/listings", headers=headers, json=body())
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def test_pausing_takes_a_live_listing_off_the_market_and_keeps_it(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Pause hides a listing; it does not end it.

    It keeps its code, its photographs and its place in the seller's history, so it can be
    put back exactly as it was.
    """
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()

    paused = client.post(f"/v1/listings/{created['id']}/pause", headers=headers)

    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "PAUSED"
    assert paused.json()["short_code"] == created["short_code"]
    assert paused.json()["version"] == created["version"], (
        "a lifecycle move is not an edit of the listing's terms, so it does not make a "
        "seller's held copy stale"
    )


def test_a_paused_listing_comes_back_to_exactly_where_it_was(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Pause and resume are a round trip, not a one-way door."""
    headers = signed_in()
    listing_id = post_listing(client, headers, listing_body)

    client.post(f"/v1/listings/{listing_id}/pause", headers=headers)
    resumed = client.post(f"/v1/listings/{listing_id}/resume", headers=headers)

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "ACTIVE"


@pytest.mark.parametrize(("move", "expected"), [("pause", "PAUSED"), ("close", "CLOSED")])
def test_a_live_listing_moves_to_exactly_one_state_per_name(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    move: str,
    expected: str,
) -> None:
    """Each of the names a live listing can be moved by means one thing and nothing else.

    Close is the one that ends it, which is what makes it the move to reach for: a seller who
    has run out, changed their mind or finished the season should not have to work out which
    word the platform expects.
    """
    headers = signed_in()
    listing_id = post_listing(client, headers, listing_body)

    response = client.post(f"/v1/listings/{listing_id}/{move}", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected


def test_a_paused_listing_can_be_closed_rather_than_resumed(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A seller giving up on a listing should not have to put it back first."""
    headers = signed_in()
    listing_id = post_listing(client, headers, listing_body)
    client.post(f"/v1/listings/{listing_id}/pause", headers=headers)

    closed = client.post(f"/v1/listings/{listing_id}/close", headers=headers)

    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "CLOSED"


@pytest.mark.parametrize(
    ("first", "second"),
    [("pause", "pause"), ("close", "close"), ("close", "resume")],
)
def test_a_move_the_state_table_forbids_comes_back_as_a_conflict(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    first: str,
    second: str,
) -> None:
    """Refused as a conflict, and the listing is left where it was.

    A second pause that quietly succeeded would leave the seller believing they had done
    something to a listing they had already stopped.
    """
    headers = signed_in()
    listing_id = post_listing(client, headers, listing_body)
    client.post(f"/v1/listings/{listing_id}/{first}", headers=headers)

    repeated = client.post(f"/v1/listings/{listing_id}/{second}", headers=headers)

    assert repeated.status_code == 409, repeated.text
    assert repeated.json()["error"]["code"] == "ILLEGAL_STATE_TRANSITION"


def test_resuming_a_listing_that_was_never_paused_is_a_conflict(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Live already: there is nothing to resume, and saying so is more use than pretending."""
    headers = signed_in()
    listing_id = post_listing(client, headers, listing_body)

    resumed = client.post(f"/v1/listings/{listing_id}/resume", headers=headers)

    assert resumed.status_code == 409, resumed.text
    assert resumed.json()["error"]["code"] == "ILLEGAL_STATE_TRANSITION"


def test_a_conflict_says_which_state_the_listing_is_actually_in(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A client refused without being told where it stands cannot recover by itself."""
    headers = signed_in()
    listing_id = post_listing(client, headers, listing_body)
    client.post(f"/v1/listings/{listing_id}/pause", headers=headers)

    refused = client.post(f"/v1/listings/{listing_id}/pause", headers=headers)

    details = refused.json()["error"]["details"]
    assert details["status"] == "PAUSED"
    assert details["requested"] == "PAUSED"


@pytest.mark.parametrize("move", ["pause", "resume", "close"])
def test_only_the_seller_may_move_their_own_listing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    move: str,
) -> None:
    """Somebody else's listing is not theirs to pause, and not theirs to close either."""
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    stranger = signed_in_as_buyer()

    refused = client.post(f"/v1/listings/{listing_id}/{move}", headers=stranger)

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "FORBIDDEN"
    unchanged = client.get(f"/v1/listings/{listing_id}", headers=seller).json()
    assert unchanged["status"] == "ACTIVE"


@pytest.mark.parametrize("move", ["pause", "resume", "close"])
def test_a_listing_that_does_not_exist_is_a_missing_resource(
    client: TestClient, signed_in: Callable[..., dict[str, str]], move: str
) -> None:
    """Not found is not forbidden, and not a conflict: there is nothing to have moved."""
    headers = signed_in()

    response = client.post(
        f"/v1/listings/1b9d6c62-0000-0000-0000-000000000000/{move}", headers=headers
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_anonymous_callers_cannot_move_anybody_s_anything(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Refused before the listing is even looked for."""
    headers = signed_in()
    listing_id = post_listing(client, headers, listing_body)

    response = client.post(f"/v1/listings/{listing_id}/close")

    assert response.status_code == 401, response.text


def test_the_moved_state_is_the_one_that_was_stored(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    """Read from the database, so this says what was written and not what was echoed."""
    headers = signed_in()
    listing_id = post_listing(client, headers, listing_body)

    client.post(f"/v1/listings/{listing_id}/pause", headers=headers)

    assert stored_listings()[0]["status"] == "PAUSED"


def test_any_signed_in_caller_may_read_a_listing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A listing is an offer to the market, so every part of the market may read it.

    Only the seller may change it; deciding whether to look is nobody's business but the
    reader's.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)

    read = client.get(f"/v1/listings/{listing_id}", headers=signed_in_as_buyer())

    assert read.status_code == 200, read.text
    assert read.json()["status"] == "ACTIVE"
