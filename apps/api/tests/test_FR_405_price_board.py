"""FR-405's price board: computed overnight, read in the morning, the same on every surface.

The board is the writer the SMS price reply and the USSD price path have been waiting for. It
is computed from the trades of the trailing thirty EAT days - funded orders and listings a
buyer could still buy - and published as one row per (crop, region, day) holding three figures:
the volume-weighted median, and the cheapest and dearest unit prices it sits between. When the
sample is too thin to say anything honest, all three are withheld together and the row says so.

This file drives the worker's own entry point against seeded observations and reads the result
back the three ways it is read: the table the worker writes, the public endpoint, and the two
trader-facing surfaces - which must quote the same number, in the unit they both claim.

The application starts inside the test's own event loop, the same shape the worker's sweeps are
tested with, because the worker and the application share one process-global engine and it is
bound to the loop its lifespan started. Seeding is done with synchronous psycopg, because the
facts being set up are the database's, not the application's.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from apps.api import worker
from apps.api.core.config import Settings
from apps.api.main import create_app
from apps.api.services.pricing_service import (
    MINIMUM_SAMPLE_SIZE,
    TRADING_WINDOW_DAYS,
    volume_weighted_median,
)
from packages.shared.messaging.queues import EAT_ZONE
from packages.shared.messaging.signature import sign_gateway_body

pytestmark = pytest.mark.integration

TERMS_VERSION = "2026-01-01"
GATEWAY_SECRET = "test-gateway-secret"

TRADER = "+251911000001"
OTHER_TRADER = "+251911000002"

#: The moment the window tests compute as if from: noon UTC on a fixed day, which is three in
#: the afternoon in Addis on the same EAT day. Fixed rather than relative to the real clock so
#: that the day a boundary names is the day the test is about, whatever day the suite runs.
NOON = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
#: The day NOON belongs to in Addis, and the day the board is therefore published for.
BOARD_DAY = date(2026, 10, 20)

#: The worked example's four observations, as (unit price in santim, quantity in kilos).
#: Three listings and one funded order. Its weighted median is 3,200 - the price of the
#: second-cheapest observation by value, and deliberately not any mean of the four.
WORKED_SALE_SANTIM = 3_200
WORKED_SALE_KG = 100
WORKED_ASK_SANTIM = (3_000, 3_300, 10_000)
WORKED_ASK_KG = (50, 200, 20)


@pytest.fixture()
async def board_app(
    settings: Settings,
    clean_database: None,
    clean_store: None,
    sms_recorder: Any,
) -> AsyncIterator[httpx.AsyncClient]:
    """The application, running inside the test's own event loop.

    The async shape is load-bearing, exactly as it is for the worker's sweep tests: the engine
    is a process-global singleton bound to the loop its lifespan started on, so the worker's
    price run and the requests it stands beside must share one loop. Driving this application
    from a synchronous client would leave the run querying a pool owned by a portal thread.
    """
    application: FastAPI = create_app(settings)
    async with application.router.lifespan_context(application):
        application.state.sms_provider = sms_recorder
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://board.test"
        ) as http:
            yield http


def _sync_url(postgres_url: str) -> str:
    return postgres_url.replace("postgresql+psycopg://", "postgresql://")


async def _sign_in(
    http: httpx.AsyncClient,
    code_for: Callable[[str], str],
    phone: str,
    *,
    language: str | None = None,
) -> dict[str, str]:
    """A registered trader, signed in against this run's application."""
    requested = await http.post("/v1/auth/otp/request", json={"phone_e164": phone})
    assert requested.status_code == 200, requested.text
    body: dict[str, str] = {
        "phone_e164": phone,
        "code": code_for(phone),
        "terms_version": TERMS_VERSION,
    }
    if language is not None:
        body["language"] = language
    verified = await http.post("/v1/auth/otp/verify", json=body)
    assert verified.status_code == 200, verified.text
    return {"Authorization": f"Bearer {verified.json()['access_token']}"}


async def _seller_in_oromia(
    http: httpx.AsyncClient,
    code_for: Callable[[str], str],
    phone: str,
    *,
    language: str | None = None,
) -> str:
    """A seller whose profile carries Oromia, and whose user id the seeds can name."""
    headers = await _sign_in(http, code_for, phone, language=language)
    updated = await http.patch(
        "/v1/me",
        headers=headers,
        json={"region_code": "OR", "woreda_id": 1},
    )
    assert updated.status_code == 200, updated.text
    return phone


async def _two_accounts(
    http: httpx.AsyncClient,
    code_for: Callable[[str], str],
    postgres_url: str,
    *,
    language: str | None = None,
) -> tuple[str, str]:
    """A seller and a buyer, both signed in, their ids returned for the seeds to name.

    Two accounts rather than one because a funded order names a buyer and the buyer must be a
    different account from the seller: an order a seller placed with themselves is not a
    trade, and the table refuses it outright.
    """
    await _seller_in_oromia(http, code_for, TRADER, language=language)
    await _sign_in(http, code_for, OTHER_TRADER)
    return _user_id(postgres_url, TRADER), _user_id(postgres_url, OTHER_TRADER)


def _user_id(postgres_url: str, phone: str) -> str:
    with psycopg.connect(_sync_url(postgres_url)) as connection:
        row = connection.execute("SELECT id FROM users WHERE phone_e164 = %s", (phone,)).fetchone()
    assert row is not None, f"no account was created for {phone}"
    return str(row[0])


def _eat_noon(day: date) -> datetime:
    """The instant at which the EAT calendar holds `day`: noon in Addis, in UTC.

    Noon rather than midnight, so a timestamp that drifted an hour either way would still be
    the day it names and the boundary under test would be the calendar's rather than the clock
    arithmetic's.
    """
    return datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC)


def _seed_listing(
    postgres_url: str,
    *,
    seller_id: str,
    unit_price_santim: int,
    quantity_kg: int,
    created_at: datetime,
    status: str = "ACTIVE",
    crop_id: int = 1,
    region_code: str = "OR",
) -> str:
    """A listing on the board, written straight into the table.

    Written rather than posted because the facts this file varies - when the listing was
    written, what state it is in - are ones the API either refuses to set or sets itself.
    """
    listing_id = str(uuid.uuid4())
    with psycopg.connect(_sync_url(postgres_url), autocommit=True) as connection:
        connection.execute(
            "INSERT INTO listings"
            " (id, seller_id, source, crop_id, quantity_kg, min_order_kg, unit_price_santim,"
            "  harvest_date, expires_at, region_code, woreda_id, delivery_options, status,"
            "  short_code, created_at)"
            " VALUES (%s, %s, 'APP', %s, %s, 1, %s, %s, %s, %s, 1,"
            "  ARRAY['PICKUP']::delivery_option[], %s, %s, %s)",
            (
                listing_id,
                seller_id,
                crop_id,
                quantity_kg,
                unit_price_santim,
                date(2026, 9, 1),
                datetime(2027, 1, 1, tzinfo=UTC),
                region_code,
                status,
                uuid.uuid4().hex[:6].upper(),
                created_at,
            ),
        )
    return listing_id


def _seed_order(
    postgres_url: str,
    *,
    seller_id: str,
    listing_id: str,
    unit_price_santim: int,
    quantity_kg: int,
    created_at: datetime,
    status: str = "FUNDED",
    funded_at: datetime | None = None,
    payment_mode: str = "ESCROW",
    buyer_id: str | None = None,
) -> str:
    """An order against a listing, written straight into the table.

    ``funded_at`` is what makes an order a sale the board can see; a cash-on-delivery order
    has no such moment, however far it has got, and this file builds one by leaving it unset.
    The buyer defaults to a stranger's id so the seed never trips the rule that a buyer and a
    seller are different people.
    """
    order_id = str(uuid.uuid4())
    with psycopg.connect(_sync_url(postgres_url), autocommit=True) as connection:
        connection.execute(
            "INSERT INTO orders"
            " (id, short_code, buyer_id, seller_id, listing_id, quantity_kg, unit_price_santim,"
            "  delivery_method, delivery_fee_santim, platform_fee_santim, vat_santim,"
            "  total_santim, fee_payer, buyer_address_woreda_id, buyer_address_text,"
            "  pickup_code, status, payment_mode, funded_at, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, 'PICKUP', 0, 0, 0, %s, 'BUYER', 1,"
            "  'Addis Ababa', %s, %s, %s, %s, %s)",
            (
                order_id,
                "GL-" + uuid.uuid4().hex[:6].upper(),
                buyer_id or str(uuid.uuid4()),
                seller_id,
                listing_id,
                quantity_kg,
                unit_price_santim,
                unit_price_santim * quantity_kg,
                f"{uuid.uuid4().int % 1_000_000:06d}",
                status,
                payment_mode,
                funded_at,
                created_at,
            ),
        )
    return order_id


def _run(settings: Settings, *, now: datetime | None = None) -> Any:
    """One run of the worker's price job, awaited by the caller."""
    return worker.run_price_index(settings, now=now)


def _published_rows(postgres_url: str) -> list[dict[str, Any]]:
    """Every board row, oldest day first, as the table holds it."""
    with psycopg.connect(_sync_url(postgres_url)) as connection:
        cursor = connection.execute(
            "SELECT crop_id, region_code, day, avg_unit_price_santim,"
            " min_unit_price_santim, max_unit_price_santim, sample_size, insufficient_data"
            " FROM price_indices ORDER BY day, crop_id, region_code"
        )
        columns = [description.name for description in cursor.description or []]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


# -- the index itself -------------------------------------------------------


def test_the_worked_example_publishes_the_index_it_promises() -> None:
    """The method's worked example, reproduced field for field.

    Four observations of teff in Oromia, three of them asking prices and one a funded sale:
    (3,000 santim, 50kg), (3,200 santim, 100kg), (3,300 santim, 200kg), (10,000 santim, 20kg).
    Weighted, the funded sale counts three times its kilos, so the weights are 50, 300, 200
    and 20 - 570 in all. Sorting by price and doubling as it accumulates, the first
    observation to reach the total is the second one, at 3,200. Every flavour of mean lands
    somewhere else: the plain mean of the four is 4,875, the volume-weighted mean 3,595, and
    the method-weighted mean 3,456. The board publishes 3,200, and no mean could.
    """
    weighted = [
        (WORKED_SALE_SANTIM, 3 * WORKED_SALE_KG),
        (WORKED_ASK_SANTIM[0], WORKED_ASK_KG[0]),
        (WORKED_ASK_SANTIM[1], WORKED_ASK_KG[1]),
        (WORKED_ASK_SANTIM[2], WORKED_ASK_KG[2]),
    ]
    assert sum(weight for _, weight in weighted) == 570
    assert volume_weighted_median(weighted) == WORKED_SALE_SANTIM
    assert volume_weighted_median(weighted) != 4_875
    assert volume_weighted_median(weighted) != 3_595
    assert volume_weighted_median(weighted) != 3_456


def test_the_median_is_an_observed_price_when_the_halfway_point_falls_in_a_weight() -> None:
    """Half the weight lands inside an observation; the price that reaches the line wins.

    The two observations weigh five each, so the total is ten and the halfway point is exactly
    five. Doubling the cheaper one's five reaches ten - the line is at that price, and the
    method returns a price somebody actually asked rather than a figure between the two.
    """
    assert volume_weighted_median([(3_000, 5), (4_000, 5)]) == 3_000


async def test_the_board_is_the_window_computed_and_published_under_the_pairs_own_names(
    board_app: httpx.AsyncClient,
    settings: Settings,
    postgres_url: str,
    code_for: Callable[[str], str],
) -> None:
    """The four observations of the worked example become one row, under (crop, region, day).

    The day is the EAT day the run happened on and the region is the listing's, not the
    order's: an order inherits the geography of what was sold.
    """
    seller, buyer = await _two_accounts(board_app, code_for, postgres_url)

    listing_ids: list[str] = []
    for price, kilos in zip(WORKED_ASK_SANTIM, WORKED_ASK_KG, strict=True):
        listing_ids.append(
            _seed_listing(
                postgres_url,
                seller_id=seller,
                unit_price_santim=price,
                quantity_kg=kilos,
                created_at=_eat_noon(BOARD_DAY - timedelta(days=1)),
            )
        )
    # The funded sale is one of those three listings sold at its own price, not a fourth
    # listing: an order is a trade, not another asking price.
    _seed_order(
        postgres_url,
        seller_id=seller,
        listing_id=listing_ids[0],
        unit_price_santim=WORKED_SALE_SANTIM,
        quantity_kg=WORKED_SALE_KG,
        created_at=_eat_noon(BOARD_DAY - timedelta(days=1)),
        funded_at=_eat_noon(BOARD_DAY - timedelta(days=1)),
        buyer_id=buyer,
    )

    assert await _run(settings, now=NOON) == 1

    rows = _published_rows(postgres_url)
    assert len(rows) == 1
    row = rows[0]
    assert row["crop_id"] == 1
    assert row["region_code"] == "OR"
    assert row["day"] == BOARD_DAY
    assert row["avg_unit_price_santim"] == 3_200
    assert row["min_unit_price_santim"] == 3_000
    assert row["max_unit_price_santim"] == 10_000
    assert row["sample_size"] == 4
    assert row["insufficient_data"] is False


async def test_two_observations_publish_a_row_that_declines_to_state_a_range(
    board_app: httpx.AsyncClient,
    settings: Settings,
    postgres_url: str,
    code_for: Callable[[str], str],
) -> None:
    """Two points are a line, not a market: the row is published, all three figures withheld.

    The pair is not left out - the board says it looked and found too little - and the two
    prices it saw are nowhere in the row, because a range of two is the misleading
    presentation the minimum exists to prevent.
    """
    await _seller_in_oromia(board_app, code_for, TRADER)
    seller = _user_id(postgres_url, TRADER)
    for price, kilos in ((3_000, 50), (10_000, 20)):
        _seed_listing(
            postgres_url,
            seller_id=seller,
            unit_price_santim=price,
            quantity_kg=kilos,
            created_at=_eat_noon(BOARD_DAY - timedelta(days=1)),
        )

    assert await _run(settings, now=NOON) == 1

    row = _published_rows(postgres_url)[0]
    assert row["sample_size"] == 2
    assert row["insufficient_data"] is True
    assert row["avg_unit_price_santim"] is None
    assert row["min_unit_price_santim"] is None
    assert row["max_unit_price_santim"] is None
    assert MINIMUM_SAMPLE_SIZE == 3


async def test_the_window_is_thirty_eat_days_ending_the_day_before(
    board_app: httpx.AsyncClient,
    settings: Settings,
    postgres_url: str,
    code_for: Callable[[str], str],
) -> None:
    """D-30 in, D-31 out, D-1 in, D out - and the exclusions would move the figures if wrong.

    The out-of-window observations carry the most extreme prices in the seed, one below
    everything and one above, so an edge measured on UTC instead of EAT - or measured
    inclusive of today - would drag a minimum or a maximum to a number no trader ever saw.
    """
    await _seller_in_oromia(board_app, code_for, TRADER)
    seller = _user_id(postgres_url, TRADER)
    day = BOARD_DAY
    seeds = (
        (day - timedelta(days=31), 100),
        (day - timedelta(days=TRADING_WINDOW_DAYS), 3_100),
        (day - timedelta(days=15), 4_000),
        (day - timedelta(days=1), 5_000),
        (day, 999_999),
    )
    for created, price in seeds:
        _seed_listing(
            postgres_url,
            seller_id=seller,
            unit_price_santim=price,
            quantity_kg=50,
            created_at=_eat_noon(created),
        )

    assert await _run(settings, now=NOON) == 1

    row = _published_rows(postgres_url)[0]
    assert TRADING_WINDOW_DAYS == 30
    assert row["day"] == day
    assert row["sample_size"] == 3
    assert row["insufficient_data"] is False
    assert row["min_unit_price_santim"] == 3_100
    assert row["max_unit_price_santim"] == 5_000
    assert row["avg_unit_price_santim"] == 4_000


async def test_a_cash_on_delivery_order_is_never_a_sale_the_board_can_see(
    board_app: httpx.AsyncClient,
    settings: Settings,
    postgres_url: str,
    code_for: Callable[[str], str],
) -> None:
    """The platform never holds COD money, so a COD order is not an observation at all.

    The ordering gives the mistake nowhere to hide: left in, the COD price of 1,000 santim
    would become both the minimum and an extra sample, and the row would report four
    observations and a range starting below every genuine one. Right, it reports three, with
    the escrow sale weighted three times its hundred kilos so that it, and not the asking
    prices, is where the halfway point falls.
    """
    seller, buyer = await _two_accounts(board_app, code_for, postgres_url)
    sale_listing = _seed_listing(
        postgres_url,
        seller_id=seller,
        unit_price_santim=3_100,
        quantity_kg=50,
        created_at=_eat_noon(BOARD_DAY - timedelta(days=1)),
    )
    _seed_listing(
        postgres_url,
        seller_id=seller,
        unit_price_santim=3_300,
        quantity_kg=50,
        created_at=_eat_noon(BOARD_DAY - timedelta(days=1)),
    )
    cod_listing = _seed_listing(
        postgres_url,
        seller_id=seller,
        unit_price_santim=1_000,
        quantity_kg=100,
        created_at=_eat_noon(BOARD_DAY - timedelta(days=1)),
        # Suspended, so the COD price cannot reach the board as an asking price either: the
        # only way this order could count is through its own existence, which is the thing
        # under test.
        status="SUSPENDED",
    )
    _seed_order(
        postgres_url,
        seller_id=seller,
        listing_id=cod_listing,
        unit_price_santim=1_000,
        quantity_kg=100,
        created_at=_eat_noon(BOARD_DAY - timedelta(days=1)),
        funded_at=None,
        status="CONFIRMED",
        payment_mode="COD",
        buyer_id=buyer,
    )
    _seed_order(
        postgres_url,
        seller_id=seller,
        listing_id=sale_listing,
        unit_price_santim=3_700,
        quantity_kg=100,
        created_at=_eat_noon(BOARD_DAY - timedelta(days=1)),
        funded_at=_eat_noon(BOARD_DAY - timedelta(days=1)),
        buyer_id=buyer,
    )

    assert await _run(settings, now=NOON) == 1

    row = _published_rows(postgres_url)[0]
    assert row["sample_size"] == 3
    assert row["insufficient_data"] is False
    assert row["min_unit_price_santim"] == 3_100
    assert row["max_unit_price_santim"] == 3_700
    assert row["avg_unit_price_santim"] == 3_700


async def test_only_what_a_buyer_can_still_reach_is_an_observation(
    board_app: httpx.AsyncClient,
    settings: Settings,
    postgres_url: str,
    code_for: Callable[[str], str],
) -> None:
    """Open and sold out count; paused, expired, suspended, draft and closed do not.

    The five excluded listings carry prices far below everything else, so any of them reaching
    the sample would move the minimum to a figure no buyer could have paid and push the count
    past three. The row reports exactly the three that were reachable.
    """
    await _seller_in_oromia(board_app, code_for, TRADER)
    seller = _user_id(postgres_url, TRADER)
    created = _eat_noon(BOARD_DAY - timedelta(days=1))
    for price in (3_100, 3_200):
        _seed_listing(
            postgres_url,
            seller_id=seller,
            unit_price_santim=price,
            quantity_kg=50,
            created_at=created,
            status="ACTIVE",
        )
    _seed_listing(
        postgres_url,
        seller_id=seller,
        unit_price_santim=3_300,
        quantity_kg=20,
        created_at=created,
        status="SOLD_OUT",
    )
    for status in ("PAUSED", "EXPIRED", "SUSPENDED", "DRAFT", "CLOSED"):
        _seed_listing(
            postgres_url,
            seller_id=seller,
            unit_price_santim=100,
            quantity_kg=50,
            created_at=created,
            status=status,
        )

    assert await _run(settings, now=NOON) == 1

    row = _published_rows(postgres_url)[0]
    assert row["sample_size"] == 3
    assert row["insufficient_data"] is False
    assert row["min_unit_price_santim"] == 3_100
    assert row["max_unit_price_santim"] == 3_300
    assert row["avg_unit_price_santim"] == 3_200


async def test_running_the_morning_job_twice_leaves_one_row(
    board_app: httpx.AsyncClient,
    settings: Settings,
    postgres_url: str,
    code_for: Callable[[str], str],
) -> None:
    """The second run of the same window overwrites the first with the same figures.

    A crashed run that is retried, or two runs that overlap, must not publish the day twice:
    the table's unique key is what makes that true, and the counts and figures are asserted
    against the table rather than against the second run's answer. The three asking prices
    weigh 50, 100 and 200, so the halfway point of 350 falls inside the dearest - the one
    figure a rerun that doubled a row or appended to it would move.
    """
    await _seller_in_oromia(board_app, code_for, TRADER)
    seller = _user_id(postgres_url, TRADER)
    created = _eat_noon(BOARD_DAY - timedelta(days=1))
    for price, kilos in ((3_000, 50), (3_200, 100), (3_300, 200)):
        _seed_listing(
            postgres_url,
            seller_id=seller,
            unit_price_santim=price,
            quantity_kg=kilos,
            created_at=created,
        )

    assert await _run(settings, now=NOON) == 1
    first = _published_rows(postgres_url)
    assert await _run(settings, now=NOON) == 1
    second = _published_rows(postgres_url)

    assert len(first) == 1
    assert second == first
    assert second[0]["avg_unit_price_santim"] == 3_300
    assert second[0]["sample_size"] == 3


async def test_a_pair_the_window_never_saw_is_not_published_at_all(
    board_app: httpx.AsyncClient,
    settings: Settings,
    postgres_url: str,
    code_for: Callable[[str], str],
) -> None:
    """The board carries rows for what it saw and nothing for what it did not.

    Amhara's teff is seeded and Oromia's is not, so the run writes one pair and leaves the
    other's answer to the absence of a row - the same "nothing to quote" the readers already
    understand.
    """
    await _seller_in_oromia(board_app, code_for, TRADER)
    seller = _user_id(postgres_url, TRADER)
    for price in (3_100, 3_200, 3_300):
        _seed_listing(
            postgres_url,
            seller_id=seller,
            unit_price_santim=price,
            quantity_kg=50,
            created_at=_eat_noon(BOARD_DAY - timedelta(days=1)),
            region_code="AM",
        )

    assert await _run(settings, now=NOON) == 1

    rows = _published_rows(postgres_url)
    assert [row["region_code"] for row in rows] == ["AM"]


# -- the endpoint -----------------------------------------------------------


async def test_the_endpoint_publishes_the_five_fields_of_the_latest_day(
    board_app: httpx.AsyncClient,
    publish_price_index: Callable[..., None],
) -> None:
    """The contract's five fields and no others, from the latest day published for the pair.

    The endpoint answers without a token, which is the point of it: a buyer with no account is
    exactly who asks whether a price is odd. Two days are published and the later one is the
    answer, so "latest" is a claim the response can be caught lying about.
    """
    today = datetime.now(EAT_ZONE).date()
    publish_price_index(
        crop_id=1,
        region_code="OR",
        average_santim=4_000,
        min_santim=3_900,
        max_santim=4_100,
        sample_size=9,
        day=today - timedelta(days=2),
    )
    publish_price_index(
        crop_id=1,
        region_code="OR",
        average_santim=3_200,
        min_santim=3_000,
        max_santim=10_000,
        sample_size=4,
        day=today,
    )

    response = await board_app.get("/v1/prices", params={"crop_id": 1, "region_code": "OR"})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "avg_price_santim": 3_200,
        "min_price_santim": 3_000,
        "max_price_santim": 10_000,
        "sample_size": 4,
        "insufficient_data": False,
    }


async def test_the_endpoint_answers_a_pair_with_no_row_rather_than_a_404(
    board_app: httpx.AsyncClient,
) -> None:
    """No published row is 200 with nothing in it, and the request is still typed.

    "There is not enough to say" is the board's own answer and a reader must be able to tell it
    from a call that went to the wrong place, so the response is a complete one with every
    figure withheld. The two refusals beside it are the request's shape rather than the
    board's: a region outside the thirteen, and a missing argument, are both turned away before
    any row is read.
    """
    response = await board_app.get("/v1/prices", params={"crop_id": 1, "region_code": "OR"})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "avg_price_santim": None,
        "min_price_santim": None,
        "max_price_santim": None,
        "sample_size": 0,
        "insufficient_data": True,
    }

    off_map = await board_app.get("/v1/prices", params={"crop_id": 1, "region_code": "ZZ"})
    assert off_map.status_code == 422
    incomplete = await board_app.get("/v1/prices", params={"region_code": "OR"})
    assert incomplete.status_code == 422


# -- the surfaces -----------------------------------------------------------


async def _sms_inbound(
    http: httpx.AsyncClient, text: str, *, from_phone: str = TRADER
) -> httpx.Response:
    """Deliver a text message the way the gateway delivers one, signed over these bytes."""
    body = urlencode({"from": from_phone, "text": text}).encode("utf-8")
    response = await http.post(
        "/v1/sms/inbound",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-AT-Signature": sign_gateway_body(GATEWAY_SECRET, body),
        },
    )
    return response


async def _ussd(
    http: httpx.AsyncClient,
    text: str,
    *,
    session_id: str,
    phone_number: str = TRADER,
) -> httpx.Response:
    """Dial a key the way a handset dials one, on a session this file opens and keeps."""
    body = urlencode({"sessionId": session_id, "phoneNumber": phone_number, "text": text}).encode(
        "utf-8"
    )
    response = await http.post(
        "/v1/ussd/session",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-AT-Signature": sign_gateway_body(GATEWAY_SECRET, body),
        },
    )
    return response


async def test_both_surfaces_quote_the_one_index_in_the_one_unit(
    board_app: httpx.AsyncClient,
    settings: Settings,
    postgres_url: str,
    code_for: Callable[[str], str],
    sms_recorder: Any,
) -> None:
    """The SMS reply and the USSD row read the same row and print the same number.

    The unit is the trap this test exists for. The board stores santim per kilogram and both
    surfaces quote birr per quintal; the two figures are equal, because a quintal is a hundred
    kilos and a birr is a hundred santim, so a reader that divided before printing - as both
    once did - would quote a hundredth of the published price while still calling it birr per
    quintal. The board is computed here rather than seeded by hand, so what is being compared
    is what the aggregation actually wrote.

    The run is given noon of the real day rather than a fixed one, because the two surfaces
    read "today" from the wall clock: at noon in UTC the EAT day and the UTC day are the same
    day, so the row the run writes is the row they look for whenever the suite runs.
    """
    seller, buyer = await _two_accounts(board_app, code_for, postgres_url, language="en")
    today = datetime.now(UTC).date()
    listing_ids: list[str] = []
    for price, kilos in zip(WORKED_ASK_SANTIM, WORKED_ASK_KG, strict=True):
        listing_ids.append(
            _seed_listing(
                postgres_url,
                seller_id=seller,
                unit_price_santim=price,
                quantity_kg=kilos,
                created_at=_eat_noon(today - timedelta(days=1)),
            )
        )
    _seed_order(
        postgres_url,
        seller_id=seller,
        listing_id=listing_ids[0],
        unit_price_santim=WORKED_SALE_SANTIM,
        quantity_kg=WORKED_SALE_KG,
        created_at=_eat_noon(today - timedelta(days=1)),
        funded_at=_eat_noon(today - timedelta(days=1)),
        buyer_id=buyer,
    )
    assert (
        await _run(
            settings, now=datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        )
        == 1
    )

    sms = await _sms_inbound(board_app, "PRICE teff")
    assert sms.status_code == 200, sms.text
    assert sms_recorder.messages_to(TRADER)[-1].body == "Today Teff: 3,200 ETB/quintal"

    opened = await _ussd(board_app, "", session_id="ses-fr405-parity")
    assert opened.status_code == 200, opened.text
    board = await _ussd(board_app, "4", session_id="ses-fr405-parity")

    assert board.text == "END Today's avg (Oromia): Teff 3,200/q"
    assert "3,200" in sms_recorder.messages_to(TRADER)[-1].body
