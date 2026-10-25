"""What a listing is made of, and which of those things it may not be.

The fields themselves and the rules about their values are both here, because for a listing
they are one thing: a row of numbers that mean nothing together is not a listing anybody can
act on. A quantity no larger than the smallest order, a delivery the seller promises to
carry for nothing, a harvest last month — each is a field that was accepted and a promise
that could not be kept.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import Response

Rows = list[dict[str, object]]


@pytest.fixture()
def sql(postgres_url: str) -> object:  # type: ignore[no-untyped-def,type-arg]
    from psycopg.rows import dict_row

    @contextmanager
    def _connect() -> Iterator:
        import psycopg

        with psycopg.connect(postgres_url, row_factory=dict_row) as conn:
            yield conn

    def _sql(statement: str, params: tuple[object, ...] | None = None) -> Rows:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(statement, params or ())
            if cur.description is None:
                return []
            return [dict(row) for row in cur.fetchall()]

    yield _sql


# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

#: Crockford Base32 leaves out I, L, O and U so a code read aloud cannot be misheard.
CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTWXYZ"


def test_a_posted_listing_says_what_the_seller_said_it_would(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A listing comes back holding the seller's own figures.

    The response is the whole row as stored, so a handset can show what was posted without
    having asked a second question.
    """
    headers = signed_in()
    body = listing_body()

    response = client.post("/v1/listings", headers=headers, json=body)

    assert response.status_code == 201, response.text
    created = response.json()
    assert created["crop_id"] == body["crop_id"]
    assert created["quantity_kg"] == body["quantity_kg"]
    assert created["min_order_kg"] == body["min_order_kg"]
    assert created["unit_price_santim"] == body["unit_price_santim"]
    assert created["harvest_date"] == body["harvest_date"]
    assert created["region_code"] == body["region_code"]
    assert created["woreda_id"] == body["woreda_id"]
    assert created["delivery_options"] == body["delivery_options"]
    assert created["delivery_fee_santim"] == 0
    assert created["description"] == body["description"]
    assert created["organic"] is False


def test_a_new_listing_is_live_and_quoted_by_a_code_of_its_own(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Posting a listing puts it on the market, under a code nothing else carries.

    Six characters, no prefix, and no letter a text message can lose on the way. An order's
    code is prefixed and longer, so the two can be told apart without stripping anything off
    either.
    """
    headers = signed_in()

    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()

    assert created["status"] == "ACTIVE", "a posted listing is for sale from the moment it exists"
    assert created["version"] == 1
    assert len(created["short_code"]) == 6
    assert set(created["short_code"]) <= set(CROCKFORD_BASE32)
    assert not created["short_code"].startswith("GL-")


def test_two_listings_are_never_quoted_by_the_same_code(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A code reaches a buyer, so it has to mean one listing and no other."""
    headers = signed_in()

    codes = {
        client.post("/v1/listings", headers=headers, json=listing_body()).json()["short_code"]
        for _ in range(5)
    }

    assert len(codes) == 5, f"a code was reused: {codes}"


def test_the_row_says_which_door_the_listing_came_in_through(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    """The source is recorded on the row rather than accepted from the request.

    Today only the app posts, so the answer is always the app. The point is that a client
    could not claim to be another channel and have it believed.
    """
    headers = signed_in()
    client.post("/v1/listings", headers=headers, json=listing_body())

    assert [row["source"] for row in stored_listings()] == ["APP"]


def test_the_channel_cannot_be_chosen_by_the_caller(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Asking to be recorded as another channel is a malformed request, not a preference."""
    headers = signed_in()

    response = client.post("/v1/listings", headers=headers, json=listing_body(source="AGENT"))

    assert response.status_code == 422, response.text


def test_a_listing_publishes_a_place_and_nothing_finer(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Region and woreda are enough for a buyer to judge collection, and no more is published.

    The trader's own point on the map, and which account an agent wrote for, stay where they
    are. Leaving them out of the shape is what keeps them out, rather than a filter that can
    be forgotten.
    """
    headers = signed_in()

    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()

    assert "region_code" in created
    assert "woreda_id" in created
    for withheld in ("seller_id", "on_behalf_of_user_id", "gps", "source"):
        assert withheld not in created, f"{withheld} is published but should not be"


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"quantity_kg": 0}, "quantity_kg"),
        ({"quantity_kg": 1_000_001}, "quantity_kg"),
        ({"min_order_kg": 0}, "min_order_kg"),
        ({"unit_price_santim": 0}, "unit_price_santim"),
        ({"min_order_kg": 2_001}, "min_order_kg"),
        ({"description": "t" * 501}, "description"),
        ({"delivery_options": []}, "delivery_options"),
    ],
)
def test_a_value_the_platform_cannot_honour_is_refused(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    overrides: dict[str, object],
    field: str,
) -> None:
    """Each of these is a field that was filled in and a promise that could not be kept.

    The field is named in the error so a handset can point at the input, and nothing is
    written, so a refusal leaves no half-posted listing behind.
    """
    headers = signed_in()

    response = client.post("/v1/listings", headers=headers, json=listing_body(**overrides))

    assert response.status_code == 422, response.text
    assert field in response.json()["error"]["details"]["fields"]


def test_the_smallest_order_cannot_exceed_the_whole_listing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A minimum above the quantity asks for more than there is."""
    headers = signed_in()

    response = client.post(
        "/v1/listings", headers=headers, json=listing_body(quantity_kg=500, min_order_kg=501)
    )

    assert response.status_code == 422, response.text
    assert "min_order_kg" in response.json()["error"]["details"]["fields"]


@pytest.mark.parametrize("days_from_today", [-1, -30, 366, 800])
def test_a_harvest_date_outside_the_next_year_is_refused(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    days_from_today: int,
) -> None:
    """A harvest cannot be in the past, and a year out is as far as a schedule can reach.

    Yesterday is somebody's typo. Three years out is not a planting schedule, and a listing
    claiming one is a date we would be publishing as though we believed it.
    """
    headers = signed_in()
    harvest = (datetime.now(UTC) + timedelta(days=days_from_today)).date().isoformat()

    response = client.post("/v1/listings", headers=headers, json=listing_body(harvest_date=harvest))

    assert response.status_code == 422, response.text
    assert "harvest_date" in response.json()["error"]["details"]["fields"]


def test_a_listing_cannot_be_posted_to_outlive_three_months(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Thirty days is the default, ninety is the ceiling, and asking for more is refused."""
    headers = signed_in()

    too_far = (datetime.now(UTC) + timedelta(days=91)).isoformat()

    response = client.post("/v1/listings", headers=headers, json=listing_body(expires_at=too_far))

    assert response.status_code == 422, response.text
    assert "expires_at" in response.json()["error"]["details"]["fields"]


def test_a_listing_posted_without_an_expiry_lives_thirty_days(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Not thinking about when should still leave a postable listing, on the default clock."""
    headers = signed_in()

    created = client.post("/v1/listings", headers=headers, json=listing_body()).json()

    expires = datetime.fromisoformat(created["expires_at"].replace("Z", "+00:00"))
    remaining = expires - datetime.now(UTC)
    assert timedelta(days=29) < remaining <= timedelta(days=30)


def test_a_delivery_the_seller_carries_is_priced_and_one_the_buyer_does_is_not(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """The fee follows the delivery it is for, in both directions.

    Carrying a delivery for nothing and charging for a collection both promise something the
    listing does not say, so both are refused rather than tidied up on the way in.
    """
    headers = signed_in()

    charged_for_pickup = client.post(
        "/v1/listings",
        headers=headers,
        json=listing_body(delivery_options=["PICKUP"], delivery_fee_santim=50_000),
    )
    free_delivery = client.post(
        "/v1/listings",
        headers=headers,
        json=listing_body(delivery_options=["SELLER_DELIVERY"], delivery_fee_santim=0),
    )
    priced = client.post(
        "/v1/listings",
        headers=headers,
        json=listing_body(
            delivery_options=["PICKUP", "SELLER_DELIVERY"], delivery_fee_santim=150_000
        ),
    )

    assert charged_for_pickup.status_code == 422, charged_for_pickup.text
    assert free_delivery.status_code == 422, free_delivery.text
    assert priced.status_code == 201, priced.text
    assert priced.json()["delivery_fee_santim"] == 150_000


def test_a_buyer_arranged_transport_carries_no_fee(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Transport the buyer organises is transport we are not paying for."""
    headers = signed_in()

    created = client.post(
        "/v1/listings",
        headers=headers,
        json=listing_body(delivery_options=["TRANSPORT_PARTNER"], delivery_fee_santim=0),
    )

    assert created.status_code == 201, created.text


def test_photographs_are_kept_in_the_order_they_were_sent(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    stored_photos: Callable[[], list[dict[str, object]]],
) -> None:
    """The seller's order is the order buyers see, and it survives being stored.

    A photograph is a reference and a position here; what the file weighs and how wide it is
    belong to the moment it is stored, which is not this slice.
    """
    headers = signed_in()
    photos = [
        {"url": f"https://cdn.gebeyalink.et/teff-{index}.jpg", "width": 1200, "height": 1200}
        for index in range(3)
    ]

    created = client.post("/v1/listings", headers=headers, json=listing_body(photos=photos))

    assert created.status_code == 201, created.text
    assert [photo["sort_order"] for photo in created.json()["photos"]] == [0, 1, 2]
    assert [row["sort_order"] for row in stored_photos()] == [0, 1, 2]


def test_five_photographs_are_allowed_and_a_sixth_is_not(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Five pictures is the set a handset takes of one lot. Six is a different lot."""
    headers = signed_in()
    photos = [
        {"url": f"https://cdn.gebeyalink.et/teff-{index}.jpg", "width": 1200, "height": 1200}
        for index in range(6)
    ]

    five = client.post("/v1/listings", headers=headers, json=listing_body(photos=photos[:5]))
    six = client.post("/v1/listings", headers=headers, json=listing_body(photos=photos))

    assert five.status_code == 201, five.text
    assert len(five.json()["photos"]) == 5
    assert six.status_code == 422, six.text


def test_what_was_posted_is_the_row_that_is_stored(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    """One row, holding what was sent, with the weight consumed still at nothing.

    Read from the database rather than from the response, so this says what was written and
    not merely what was echoed back.
    """
    headers = signed_in()
    client.post("/v1/listings", headers=headers, json=listing_body())

    rows = stored_listings()
    assert len(rows) == 1
    row = rows[0]
    assert row["quantity_kg"] == 2_000
    assert row["sold_kg"] == 0
    assert row["status"] == "ACTIVE"
    assert row["version"] == 1
    assert row["price_outlier"] is False
    assert row["organic"] is False


def test_fr_302_all_channels_write_source_and_honour_constraints(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    stored_listings: Callable[[], list[dict[str, object]]],
    sql: Callable[..., list[dict[str, object]]],
    code_for: Callable[[str], str],
    ussd_session: Callable[..., Response],
) -> None:
    """FR-302's matrix: each channel produces a listing with its own source and the constraint the requirement names for it."""  # noqa: E501

    from apps.api.tests.test_FR_1101_ussd_create_listing import (
        SELLER as USSD_SELLER,
    )
    from apps.api.tests.test_FR_1101_ussd_create_listing import (
        _ready,
        _walk,
    )
    from apps.api.tests.test_FR_1102_sms_inbound_commands import (
        SELLER as SMS_SELLER,
    )
    from apps.api.tests.test_FR_1102_sms_inbound_commands import (
        _as_seller,
    )
    from apps.api.tests.test_FR_1201_1203_1707_agent_basics import (
        AGENT_PHONE,
        FARMER_PHONE,
        _onboard,
    )

    before_app = len(stored_listings())
    headers_app = signed_in()
    body_app = listing_body(
        photos=[{"url": "https://cdn.gebeyalink.et/teff-app.jpg", "width": 1200, "height": 1200}]
    )
    resp_app = client.post("/v1/listings", headers=headers_app, json=body_app)
    assert resp_app.status_code == 201
    app_rows = stored_listings()[before_app:]
    assert len(app_rows) == 1
    assert app_rows[0]["source"] == "APP"
    # photo row exists for APP
    photos_all = sql("SELECT * FROM listing_photos")
    assert len(photos_all) >= 1

    before_ussd = len(stored_listings())
    _ready(client, signed_in, USSD_SELLER)

    ussd_replies = _walk(
        client,
        ussd_session,
        ["1", "1", "1", "500", "2500", "1", "1"],
        session_id="ses-fr302-ussd",
        phone_number=USSD_SELLER,
    )
    assert isinstance(ussd_replies, list)
    assert getattr(ussd_replies[-1], "text", "").startswith("END Listing ")
    ussd_rows = stored_listings()[before_ussd:]
    assert len(ussd_rows) == 1
    assert ussd_rows[0]["source"] == "USSD"
    # USSD: no photos
    ussd_id = ussd_rows[0]["id"]
    ussd_photos = sql("SELECT * FROM listing_photos WHERE listing_id = %s", (ussd_id,))
    assert ussd_photos == []

    before_sms = len(stored_listings())
    _as_seller(client, signed_in)
    from apps.api.tests.conftest import sms_inbound

    sms_resp = sms_inbound()(client, "POST teff 1500 30", from_phone=SMS_SELLER)
    assert sms_resp.status_code == 200
    sms_rows = stored_listings()[before_sms:]
    assert len(sms_rows) == 1
    assert sms_rows[0]["source"] == "SMS"
    # SMS constraints per §14.4
    assert sms_rows[0]["min_order_kg"] == 1
    assert "PICKUP" in str(sms_rows[0]["delivery_options"])
    # harvest_date today (EAT) - approximate
    harvest = sms_rows[0]["harvest_date"]
    assert harvest is not None

    before_agent = len(stored_listings())
    agent_headers = signed_in(AGENT_PHONE)
    confirmed = _onboard(client, agent_headers, code_for, FARMER_PHONE)
    farmer_id = confirmed.json()["id"]
    posted = client.post(
        "/v1/listings",
        json={**listing_body(), "on_behalf_of_user_id": farmer_id},
        headers=agent_headers,
    )
    assert posted.status_code == 201
    agent_rows = stored_listings()[before_agent:]
    assert len(agent_rows) == 1
    assert agent_rows[0]["source"] == "AGENT"
    assert str(agent_rows[0]["seller_id"]) == farmer_id
    assert str(agent_rows[0]["on_behalf_of_user_id"]) == farmer_id
