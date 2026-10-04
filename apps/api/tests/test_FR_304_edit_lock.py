"""Correcting a listing that has already been posted.

Two sellers' devices, or a seller and a stale screen, will disagree about what a listing says
at the same moment. The rule is that whoever writes second is told what is true now, rather
than being allowed to overwrite what the other one wrote. These are the checks that make the
correction safe rather than a way of losing a change.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration


def test_a_seller_may_correct_a_listing_they_posted(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """The ordinary correction: the held version is current, so the edit stands."""
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()

    edited = client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"unit_price_santim": 2_500_000, "version": created["version"]},
    )

    assert edited.status_code == 200, edited.text
    assert edited.json()["unit_price_santim"] == 2_500_000


def test_a_change_that_succeeds_moves_the_version_on_by_one(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """The number a client sends back is the number it last saw.

    Nothing else can serve that purpose: the seller's intent is the only thing the server
    has, and a version that does not move after a change cannot catch a second change.
    """
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()

    edited = client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"unit_price_santim": 2_500_000, "version": created["version"]},
    )

    assert edited.json()["version"] == created["version"] + 1


def test_a_change_that_changes_nothing_leaves_the_version_alone(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Sending the same values back is not a change.

    Bumping the version for it would invalidate every other screen the seller has open for a
    submission that did nothing, which teaches clients to retry on conflicts they were not
    actually in.
    """
    headers = signed_in()
    body = listing_body()
    created = client.post("/v1/listings", headers=headers, json=body).json()

    repeated = client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"unit_price_santim": body["unit_price_santim"], "version": created["version"]},
    )

    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["version"] == created["version"]


def test_a_seller_who_edited_on_an_old_screen_is_told_and_given_what_is_true(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Refused as a conflict, and the response carries the listing as it now stands.

    The second edit is not lost silently and not applied blindly: the seller is handed the
    current row so they can decide what they meant, which is the only way out that keeps both
    changes.
    """
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    first = client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"unit_price_santim": 2_500_000, "version": created["version"]},
    ).json()

    stale = client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"unit_price_santim": 9_000_000, "version": created["version"]},
    )

    assert stale.status_code == 409, stale.text
    error = stale.json()["error"]
    assert error["code"] == "CONCURRENT_MODIFICATION"
    assert error["details"]["listing_id"] == created["id"]
    assert error["details"]["expected_version"] == created["version"]
    assert error["details"]["current"]["version"] == first["version"]
    assert error["details"]["current"]["unit_price_santim"] == first["unit_price_santim"]


def test_the_refused_change_is_not_applied_behind_the_refusal(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A conflict is a refusal, not a warning.

    Applying the write and then complaining would leave the seller believing their price was
    rejected when in fact it is live.
    """
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"unit_price_santim": 2_500_000, "version": created["version"]},
    )

    client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"unit_price_santim": 9_000_000, "version": created["version"]},
    )
    read = client.get(f"/v1/listings/{created['id']}", headers=headers).json()

    assert read["unit_price_santim"] == 2_500_000


def test_a_seller_may_keep_editing_after_being_told_where_they_are(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """The conflict response is usable: its version is the one the next edit must carry.

    A refusal that could not be followed up would make every collision a dead end.
    """
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"unit_price_santim": 2_500_000, "version": created["version"]},
    )
    refused = client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"unit_price_santim": 9_000_000, "version": created["version"]},
    ).json()

    retried = client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={
            "unit_price_santim": 9_000_000,
            "version": refused["error"]["details"]["current"]["version"],
        },
    )

    assert retried.status_code == 200, retried.text
    assert retried.json()["unit_price_santim"] == 9_000_000


def test_an_edit_may_not_change_the_terms_of_the_offer_itself(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """The crop, the place and the quantity are what a buyer agreed to; the rest is not.

    Swapping any of them after posting would make the offer a different one from the one
    that was answered, so they are not editable at all rather than editable and hoped for.
    """
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()

    edited = client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"crop_id": 2, "version": created["version"]},
    )

    assert edited.status_code == 422, edited.text


def test_a_closed_listing_cannot_be_corrected_afterwards(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Closing ends the row, and with it the right to change what it said."""
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    client.post(f"/v1/listings/{created['id']}/close", headers=headers)

    edited = client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"unit_price_santim": 2_500_000, "version": created["version"]},
    )

    assert edited.status_code == 409, edited.text


def test_a_photograph_may_be_swapped_for_another(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A better picture of the same crop is the most common correction there is."""
    headers = signed_in()
    body = listing_body(
        photos=[{"url": "https://images.example.com/teff-old.jpg", "width": 1200, "height": 1200}]
    )
    created = client.post("/v1/listings", headers=headers, json=body)

    assert created.status_code == 201, created.text
    posted = created.json()
    edited = client.patch(
        f"/v1/listings/{posted['id']}",
        headers=headers,
        json={
            "photos": [
                {"url": "https://images.example.com/teff-new.jpg", "width": 1200, "height": 1200},
                {"url": "https://images.example.com/teff-bale.jpg", "width": 900, "height": 900},
            ],
            "version": posted["version"],
        },
    )

    assert edited.status_code == 200, edited.text
    assert [photo["url"] for photo in edited.json()["photos"]] == [
        "https://images.example.com/teff-new.jpg",
        "https://images.example.com/teff-bale.jpg",
    ]
    assert [photo["sort_order"] for photo in edited.json()["photos"]] == [0, 1]


def test_an_edit_is_checked_against_every_rule_a_post_is(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Correcting a listing to something invalid is refused exactly as posting it would be.

    An edit path that checked less than the create path would be a way around every rule
    about quantities, dates and prices.
    """
    headers = signed_in()
    created = client.post(
        "/v1/listings", headers=headers, json=listing_body(quantity_kg=100)
    ).json()

    too_large_a_minimum = client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"min_order_kg": 500, "version": created["version"]},
    )
    zero_price = client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"unit_price_santim": 0, "version": created["version"]},
    )

    assert too_large_a_minimum.status_code == 422, too_large_a_minimum.text
    assert zero_price.status_code == 422, zero_price.text


def test_a_refused_edit_leaves_the_listing_readable_and_unchanged(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """An invalid edit is a refusal with no other effect on the row."""
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()

    client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"unit_price_santim": -1, "version": created["version"]},
    )
    read = client.get(f"/v1/listings/{created['id']}", headers=headers).json()

    assert read["unit_price_santim"] == created["unit_price_santim"]
    assert read["version"] == created["version"]


@pytest.mark.parametrize("field", ["unit_price_santim", "quantity_kg", "description"])
def test_somebody_elses_listing_cannot_be_corrected_by_anybody(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    field: str,
) -> None:
    """Forbidden, for every field a seller may edit, and the listing is left as it was.

    Each field is sent on its own and valid, so that what is refused is the ownership and not
    the shape of the request.
    """
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    proposed = {
        "unit_price_santim": 1_000,
        "quantity_kg": 400,
        "description": "somebody else's text",
    }

    refused = client.patch(
        f"/v1/listings/{created['id']}",
        headers=signed_in_as_buyer(),
        json={field: proposed[field], "version": created["version"]},
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "FORBIDDEN"
    assert client.get(f"/v1/listings/{created['id']}", headers=headers).json() == created


def test_an_edit_needs_a_version_to_be_honest_about(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Refused as a validation failure rather than assumed to be current.

    An edit without a version is an unconditional overwrite, which is the thing this rule
    exists to prevent, so it is not a request the server will guess at.
    """
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()

    response = client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"unit_price_santim": 2_500_000},
    )

    assert response.status_code == 422, response.text
    assert created["version"] == 1


def test_a_paused_listing_can_still_be_corrected(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Paused is not closed.

    A seller who paused to go and check the market should not have to bring a listing back
    into the world just to fix a typo in it.
    """
    headers = signed_in()
    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()
    client.post(f"/v1/listings/{created['id']}/pause", headers=headers)

    edited = client.patch(
        f"/v1/listings/{created['id']}",
        headers=headers,
        json={"unit_price_santim": 2_500_000, "version": created["version"]},
    )

    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "PAUSED"
    assert edited.json()["unit_price_santim"] == 2_500_000
