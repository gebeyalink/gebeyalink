"""Reading an order: its code, its pickup code, and who is allowed to ask.

FR-608 is about a code a human reads. The short code is stored with its `GL-` prefix rather than
having one added at display time, so an SMS, a USSD menu and an API response are all showing the
same stored string and cannot drift apart; the characters come from Crockford's alphabet, which
leaves out the letters that sound alike or read alike, because a code misheard on a telephone line
is a code the seller cannot match to an order.

The reads are also where the order's privacy is enforced. An order is the only row in the platform
that names two accounts, and it is also where a buyer's delivery address meets a seller's committed
stock, so a third party reading one would be a data leak rather than a preference. The tests below
check both that the two parties can read and that nobody else can, including through the list, where
the authorization is the caller's own `?as=` parameter rather than a check.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

# SPEC A20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

#: `GL-` and six characters from Crockford's base 32, which omits I, L, O and U.
SHORT_CODE = re.compile(r"^GL-[0-9A-HJKMNP-TV-Z]{6}$")
PICKUP_CODE = re.compile(r"^[0-9]{6}$")

BUY_KG = 500


def post_listing(
    client: TestClient,
    headers: dict[str, str],
    body: Callable[..., dict[str, object]],
    **overrides: object,
) -> str:
    """Post a listing and hand back its identity."""
    response = client.post("/v1/listings", headers=headers, json=body(**overrides))
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def buy_now(
    client: TestClient,
    headers: dict[str, str],
    listing_id: str,
    body: Callable[..., dict[str, object]],
    key: Callable[[], str],
    **overrides: object,
) -> dict[str, object]:
    """Buy now and hand back the created order."""
    response = client.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**headers, "Idempotency-Key": key()},
        json=body(**overrides),
    )
    assert response.status_code == 201, response.text
    order: dict[str, object] = response.json()
    return order


def test_the_short_code_is_the_stored_string_and_it_is_readable_aloud(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The prefix is part of the code, and the code is what every surface shows.

    Stored rather than displayed means the value in the response, the value in the row and the
    value in the text message a buyer would be sent are one string, so there is nowhere for the
    three to disagree. The alphabet is asserted as well as the shape, because a `GL-` prefix with
    an `I` or an `O` in it would satisfy a length check and still be a code that gets misheard.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")

    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)

    assert SHORT_CODE.match(str(order["short_code"])), order["short_code"]
    assert stored_orders()[0]["short_code"] == order["short_code"]
    assert PICKUP_CODE.match(str(order["pickup_code"])), order["pickup_code"]


def test_two_orders_do_not_share_a_code(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """A code that named two orders would name none of them.

    The codes are drawn one at a time and checked against the table before the row is written, so
    this is checking the whole mechanism rather than luck: six characters of thirty bits collide
    about once in a billion, and a suite that saw two the same would be looking at a generator
    that was not asking.
    """
    seller = signed_in()
    first_listing = post_listing(client, seller, listing_body)
    second_listing = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")

    first = buy_now(client, buyer, first_listing, order_body, idempotency_key, quantity_kg=BUY_KG)
    second = buy_now(client, buyer, second_listing, order_body, idempotency_key, quantity_kg=BUY_KG)

    assert first["short_code"] != second["short_code"]
    assert first["pickup_code"] != second["pickup_code"]


def test_both_parties_can_read_an_order_and_nobody_else_can(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The buyer and the seller, and the distinction between them is the whole point.

    The two parties to an order read the same row, and the one field they do not read the same
    is the pickup code: it is how a seller verifies the handover, and a code the party being
    verified can see is a code the verification proves nothing about — so the seller's copy
    carries `None` where the buyer's carries the code, and the two reads are otherwise
    identical.

    403 rather than 404 for the stranger, and deliberately: the short code in a text message is
    the only route anybody has to an order, so a caller who may not read a row still needs to be
    able to tell a mistyped identity from one that exists. Existence is answered first for exactly
    that reason.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)
    stranger = signed_in("+251911000004")

    as_buyer = client.get(f"/v1/orders/{order['id']}", headers=buyer)
    as_seller = client.get(f"/v1/orders/{order['id']}", headers=seller)
    as_stranger = client.get(f"/v1/orders/{order['id']}", headers=stranger)

    assert as_buyer.status_code == 200, as_buyer.text
    assert as_seller.status_code == 200, as_seller.text
    buyer_body = as_buyer.json()
    seller_body = as_seller.json()
    assert PICKUP_CODE.match(str(buyer_body["pickup_code"])), buyer_body["pickup_code"]
    assert seller_body["pickup_code"] is None
    assert buyer_body.pop("pickup_code") is not None
    assert seller_body.pop("pickup_code") is None
    assert buyer_body == seller_body
    assert as_stranger.status_code == 403, as_stranger.text
    assert as_stranger.json()["error"]["details"]["reason"] == "not_a_party_to_this_order"


def test_an_order_that_is_not_there_is_a_404_even_for_its_own_buyer(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
) -> None:
    """A signed-in caller with a made-up identity gets a 404, not a 403.

    The two are different answers and mixing them up would leak which identities exist: a 403 for
    every missing order would tell a stranger nothing, and a 403 for a real one would tell them
    something. The order of the two checks is the whole of the difference, so both halves are
    asserted.
    """
    buyer = signed_in("+251911000003")

    response = client.get(f"/v1/orders/{uuid.uuid4()}", headers=buyer)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["details"]["resource"] == "order"


def test_orders_mine_reads_one_side_and_nobody_elses(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The list is authorized by the same rule as the read, and the rule is the columns.

    `?as=` is the caller's to state and each value reads a different column, so a seller asking for
    their buyer side gets nothing rather than somebody else's buyers. The stranger's two empty
    pages are the part worth stating: an order the caller is not on appears in neither list, so a
    leak here would be a leak of both addresses at once.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    stranger = signed_in("+251911000004")
    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)

    as_buyer = client.get("/v1/orders/mine", headers=buyer, params={"as": "buyer"})
    as_seller = client.get("/v1/orders/mine", headers=seller, params={"as": "seller"})
    buyer_asking_as_seller = client.get("/v1/orders/mine", headers=buyer, params={"as": "seller"})
    stranger_buying = client.get("/v1/orders/mine", headers=stranger, params={"as": "buyer"})
    stranger_selling = client.get("/v1/orders/mine", headers=stranger, params={"as": "seller"})

    assert as_buyer.status_code == 200, as_buyer.text
    assert [row["id"] for row in as_buyer.json()["data"]] == [order["id"]]
    assert [row["id"] for row in as_seller.json()["data"]] == [order["id"]]
    assert buyer_asking_as_seller.json()["data"] == []
    assert stranger_buying.json()["data"] == []
    assert stranger_selling.json()["data"] == []
    assert as_buyer.json()["has_more"] is False
    assert as_buyer.json()["next_cursor"] is None


def test_the_side_and_the_status_are_both_filters_and_the_page_walks_with_a_cursor(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Keyset pagination, and the two filters a trader's history screen actually needs.

    The cursor is walked rather than counted because the alternative is what offset pagination is
    wrong about: an order created while somebody is reading their first page must not push a row
    they have already seen onto the next one. Two pages of one, with the second holding the older
    order, is what makes the cursor's direction observable.
    """
    seller = signed_in()
    first_listing = post_listing(client, seller, listing_body)
    second_listing = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    older = buy_now(client, buyer, first_listing, order_body, idempotency_key, quantity_kg=BUY_KG)
    newer = buy_now(client, buyer, second_listing, order_body, idempotency_key, quantity_kg=BUY_KG)

    first_page = client.get("/v1/orders/mine", headers=buyer, params={"as": "buyer", "limit": 1})
    assert first_page.status_code == 200, first_page.text
    page = first_page.json()
    assert [row["id"] for row in page["data"]] == [newer["id"]]
    assert page["has_more"] is True
    cursor = page["next_cursor"]
    assert cursor

    second_page = client.get(
        "/v1/orders/mine",
        headers=buyer,
        params={"as": "buyer", "limit": 1, "cursor": cursor},
    )
    assert [row["id"] for row in second_page.json()["data"]] == [older["id"]]
    assert second_page.json()["has_more"] is False
    assert second_page.json()["next_cursor"] is None

    cancelled = client.post(
        f"/v1/orders/{older['id']}/cancel",
        headers={**buyer, "Idempotency-Key": idempotency_key()},
    )
    assert cancelled.status_code == 200, cancelled.text

    waiting = client.get(
        "/v1/orders/mine",
        headers=buyer,
        params={"as": "buyer", "status": "AWAITING_FUNDING"},
    )
    cancelled_only = client.get(
        "/v1/orders/mine", headers=buyer, params={"as": "buyer", "status": "CANCELLED"}
    )
    assert [row["id"] for row in waiting.json()["data"]] == [newer["id"]]
    assert [row["id"] for row in cancelled_only.json()["data"]] == [older["id"]]


def test_the_list_needs_a_side_and_the_page_size_has_a_ceiling(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
) -> None:
    """`?as=` is required, because the platform will not guess which side a caller means.

    Defaulting it would mean a buyer with orders as both a buyer and a seller being shown one of
    them and told it was all of them. The ceiling is on the page rather than on the history: a
    trader with years of orders should page, and a phone should not be asked to render all of it
    at once.
    """
    buyer = signed_in("+251911000003")

    no_side = client.get("/v1/orders/mine", headers=buyer)
    wrong_side = client.get("/v1/orders/mine", headers=buyer, params={"as": "seller"})
    too_big = client.get("/v1/orders/mine", headers=buyer, params={"as": "buyer", "limit": 101})
    unknown_status = client.get(
        "/v1/orders/mine", headers=buyer, params={"as": "buyer", "status": "SOLD"}
    )
    unknown_order = client.get(f"/v1/orders/{buyer['Authorization']}", headers=buyer)

    assert no_side.status_code == 422, no_side.text
    assert wrong_side.status_code == 200, wrong_side.text
    assert too_big.status_code == 422, too_big.text
    assert unknown_status.status_code == 422, unknown_status.text
    # `/orders/mine` is a route rather than `/orders/{order_id}` matching the word "mine" as an
    # identity, so the list is reachable and the order route is the one that complains about
    # UUIDs. A regression here would answer 422 about a UUID instead of returning the page.
    assert unknown_order.status_code == 422, unknown_order.text
    assert "uuid" in unknown_order.text.lower()


def test_reading_needs_no_idempotency_key_because_it_writes_nothing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """A read is a read, and the key is a promise about mutations only.

    Asserted because it is the easy thing to get wrong when a header is added to a router: a read
    that demanded an idempotency key would be asking a client to invent one before it could show
    a buyer their own order, and a key invented per read would be the client caching nothing.
    """
    seller = signed_in()
    listing_id = post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    order = buy_now(client, buyer, listing_id, order_body, idempotency_key, quantity_kg=BUY_KG)

    response = client.get(f"/v1/orders/{order['id']}", headers=buyer)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == order["id"]
