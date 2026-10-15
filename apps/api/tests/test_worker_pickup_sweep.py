"""The worker settling a lapsed pickup, rather than the reader who used to.

A pickup that has sat ready for a day is deemed collected: the buyer neither came nor complained,
so the goods waited at the seller's cost. That judgement is the clock's whether or not anything
has got round to writing it down, and this file is about the half of the arrangement that writes:
a worker on a cadence, rather than whichever reader happened to look. The read still derives the
same status it always did — the difference the migration makes is *when* the row and the
reservation are persisted, not what a user sees.

Two things are proved here and nothing else. First, the settlement itself: the same transition,
the same consumption, the pickup code cleared by the same transaction, one bounded batch per run,
and the sweep's SQL boundary answered identically by the predicate every reader uses. Second, the
hard part of a second writer — two processes now race over the same pickups, and exactly one
settlement must come out of every race, whether the second contestant is another sweep or the
seller's own confirm-pickup.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from apps.api import worker
from apps.api.core.config import Settings
from apps.api.db.session import get_session_factory
from apps.api.main import create_app
from apps.api.repositories.order_repository import OrderRepository
from apps.api.services.order_service import OrderService, pickup_window_closed

# SPEC A20: this file drives the real app against an ephemeral Postgres, so it is an
# integration-layer test.
pytestmark = pytest.mark.integration

#: The terms this file's accounts accept. The API gives the value no meaning; it is required
#: because a handset must send one, and a body without it is a different test.
TERMS_VERSION = "2026-01-01"

SELLER_PHONE = "+251911000011"
BUYER_PHONE = "+251911000013"

CHEAP_LISTING_KG = 2_000
CHEAP_ASK_SANTIM = 1_000
COD_BUY_KG = 200


@pytest.fixture()
async def cod_app(
    cod_settings: Settings,
    clean_database: None,
    clean_store: None,
    sms_recorder: Any,
) -> Any:
    """The COD-enabled application, running inside the test's own event loop.

    The async shape is load-bearing. The engine and the store are process-global singletons bound
    to the loop their application's lifespan started on, so the worker's sweep and the requests it
    stands beside must share one loop. Driving this application from a synchronous client would
    leave the sweep querying a connection pool owned by a portal thread.
    """
    application: FastAPI = create_app(cod_settings)
    async with application.router.lifespan_context(application):
        application.state.sms_provider = sms_recorder
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://cod.test"
        ) as http:
            yield http


def _sweep(settings: Settings) -> Awaitable[int]:
    """One run of the worker's pickup sweep, awaited by the caller."""
    return worker.run_pickup_sweep(settings)


def _sync_url(postgres_url: str) -> str:
    return postgres_url.replace("postgresql+psycopg://", "postgresql://")


def _set_role(postgres_url: str, phone: str, role: str) -> None:
    with psycopg.connect(_sync_url(postgres_url), autocommit=True) as connection:
        connection.execute("UPDATE users SET role = %s WHERE phone_e164 = %s", (role, phone))


def _set_ready_at(postgres_url: str, order_id: str, moment: datetime) -> None:
    """Write the one column the pickup window is measured from, to the second."""
    with psycopg.connect(_sync_url(postgres_url), autocommit=True) as connection:
        connection.execute("UPDATE orders SET ready_at = %s WHERE id = %s", (moment, order_id))


async def _sign_in(
    http: httpx.AsyncClient,
    code_for: Callable[[str], str],
    phone: str,
    *,
    role: str | None = None,
    postgres_url: str | None = None,
) -> dict[str, str]:
    requested = await http.post("/v1/auth/otp/request", json={"phone_e164": phone})
    assert requested.status_code == 200, requested.text
    verified = await http.post(
        "/v1/auth/otp/verify",
        json={"phone_e164": phone, "code": code_for(phone), "terms_version": TERMS_VERSION},
    )
    assert verified.status_code == 200, verified.text
    headers = {"Authorization": f"Bearer {verified.json()['access_token']}"}
    if role is not None:
        assert postgres_url is not None
        _set_role(postgres_url, phone, role)
    return headers


async def _parties(
    http: httpx.AsyncClient,
    code_for: Callable[[str], str],
    postgres_url: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """A seller and a buyer, signed in against this run's COD application."""
    seller = await _sign_in(http, code_for, SELLER_PHONE)
    buyer = await _sign_in(http, code_for, BUYER_PHONE, role="BUYER", postgres_url=postgres_url)
    return seller, buyer


async def _ready_pickup_order(
    http: httpx.AsyncClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
) -> str:
    """Stand up a COD pickup order and walk it to ready, behind no clock."""
    posted = await http.post(
        "/v1/listings",
        headers=seller,
        json=listing_body(
            quantity_kg=CHEAP_LISTING_KG,
            unit_price_santim=CHEAP_ASK_SANTIM,
            delivery_options=["PICKUP"],
            delivery_fee_santim=0,
        ),
    )
    assert posted.status_code == 201, posted.text
    listing_id = posted.json()["id"]

    bought = await http.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**buyer, "Idempotency-Key": str(uuid.uuid4())},
        json=order_body(
            payment_mode="COD",
            quantity_kg=COD_BUY_KG,
            delivery_method="PICKUP",
        ),
    )
    assert bought.status_code == 201, bought.text
    order_id = str(bought.json()["id"])

    for confirmer in (buyer, seller):
        confirmed = await http.post(f"/v1/orders/{order_id}/confirm", headers=confirmer)
        assert confirmed.status_code == 200, confirmed.text

    marked = await http.post(
        f"/v1/orders/{order_id}/ready",
        headers={**seller, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert marked.status_code == 200, marked.text
    return order_id


async def _pickup_code(http: httpx.AsyncClient, buyer: dict[str, str], order_id: str) -> str:
    """The buyer's copy of the code, read from the API the way the app itself would."""
    read = await http.get(f"/v1/orders/{order_id}", headers=buyer)
    assert read.status_code == 200, read.text
    code = read.json()["pickup_code"]
    assert code is not None, read.text
    return str(code)


def _row(orders: list[dict[str, object]], order_id: str) -> dict[str, object]:
    return next(order for order in orders if str(order["id"]) == order_id)


async def test_the_worker_settles_a_lapsed_pickup(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_ready: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """The worker writes what the read derives: delivered, by the clock, and consumed.

    The status, the stamp, the mechanism and the reservation state are all read back from the
    database, because the point of moving the write to a worker is that the row is now the record
    and not merely the answer to whoever asked.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    order_id = await _ready_pickup_order(cod_app, seller, buyer, listing_body, order_body)
    backdate_ready(order_id)

    assert await _sweep(cod_settings) == 1

    row = _row(stored_orders(), order_id)
    assert row["status"] == "DELIVERED"
    assert row["delivered_via"] == "PICKUP_WINDOW"
    assert row["delivered_at"] is not None
    assert stored_reservations()[0]["state"] == "CONSUMED"


async def test_the_worker_clears_the_pickup_code_when_it_settles(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_ready: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The code is spent by the settlement, and it is the settler that spends it.

    `_settle_delivery` writes the status, the stamp and the mechanism and touches nothing else,
    so clearing the code is the caller's job -- the same way the seller's own confirmation spends
    it. The code is read back non-null before the sweep and null after it, because a code left
    behind is a code a seller could be shown twice for one lot of goods.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    order_id = await _ready_pickup_order(cod_app, seller, buyer, listing_body, order_body)
    code = await _pickup_code(cod_app, buyer, order_id)
    assert code
    backdate_ready(order_id)

    assert _row(stored_orders(), order_id)["pickup_code"] == code

    assert await _sweep(cod_settings) == 1

    assert _row(stored_orders(), order_id)["pickup_code"] is None


async def test_the_worker_settles_nothing_twice(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_ready: Callable[[str], None],
) -> None:
    """A second run finds nothing: the order is no longer ready to be selected."""
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    order_id = await _ready_pickup_order(cod_app, seller, buyer, listing_body, order_body)
    backdate_ready(order_id)

    assert await _sweep(cod_settings) == 1
    assert await _sweep(cod_settings) == 0


async def test_the_worker_settles_at_most_one_bounded_batch(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_ready: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hundred is a promise about one run, and this is that promise made small.

    Three pickups have lapsed and the run is allowed two, so exactly two go and one waits for the
    next cadence. The bound is what keeps the worker's first statement a short one.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    first = await _ready_pickup_order(cod_app, seller, buyer, listing_body, order_body)
    second = await _ready_pickup_order(cod_app, seller, buyer, listing_body, order_body)
    third = await _ready_pickup_order(cod_app, seller, buyer, listing_body, order_body)
    for order_id in (first, second, third):
        backdate_ready(order_id)

    monkeypatch.setattr("apps.api.services.order_service.PICKUP_SWEEP_BATCH_SIZE", 2)

    assert await _sweep(cod_settings) == 2

    delivered = [
        order_id
        for order_id in (first, second, third)
        if _row(stored_orders(), order_id)["status"] == "DELIVERED"
    ]
    assert len(delivered) == 2


async def test_a_failure_on_one_order_does_not_abandon_the_sweep(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_ready: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One broken order does not cost the others their settlement.

    The sweep writes one order per transaction, so a raise is that order's transaction rolled
    back and nothing more: the next order keeps its turn with a session that is clean again.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    broken = await _ready_pickup_order(cod_app, seller, buyer, listing_body, order_body)
    intact = await _ready_pickup_order(cod_app, seller, buyer, listing_body, order_body)
    backdate_ready(broken)
    backdate_ready(intact)

    settled = OrderService._settle_delivery

    async def _flaky(self: OrderService, order: Any, *args: Any, **kwargs: Any) -> Any:
        if str(order.id) == broken:
            raise RuntimeError("this settlement is broken on purpose")
        return await settled(self, order, *args, **kwargs)

    monkeypatch.setattr(OrderService, "_settle_delivery", _flaky)

    assert await _sweep(cod_settings) == 1

    assert _row(stored_orders(), broken)["status"] == "READY_FOR_PICKUP"
    assert _row(stored_orders(), intact)["status"] == "DELIVERED"


async def test_the_worker_s_boundary_is_the_read_path_s_boundary(
    cod_app: httpx.AsyncClient,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
) -> None:
    """The sweep's SQL and the read's Python answer the same question the same way.

    The window has two expressions in this codebase -- `pickup_window_closed` for every reader and
    the repository's query for the worker -- and a second writer that left them disagreeing by a
    second would settle a pickup the read still calls ready, or miss one it calls delivered. Three
    pickups are placed one second inside, exactly on, and one second outside the boundary, and
    each answer is compared across both expressions.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)

    now = datetime.now(UTC)
    inside = now - timedelta(hours=24) + timedelta(seconds=1)
    on = now - timedelta(hours=24)
    outside = now - timedelta(hours=24) - timedelta(seconds=1)

    for ready_at, expected in ((inside, False), (on, True), (outside, True)):
        order_id = await _ready_pickup_order(cod_app, seller, buyer, listing_body, order_body)
        _set_ready_at(postgres_url, order_id, ready_at)

        async with get_session_factory()() as session:
            orders = OrderRepository(session)
            lapsed = await orders.list_lapsed_pickup_ids(now, limit=100)
            selected = order_id in {str(candidate) for candidate in lapsed}
            stored = await orders.find_by_id(uuid.UUID(order_id))
        assert stored is not None
        derived = pickup_window_closed(stored, now)

        assert derived is expected
        assert selected is expected


async def test_two_workers_racing_settle_once(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_ready: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """Two sweeps over one lapsed pickup settle it once between them.

    The barrier is what makes this a race rather than two calls in a row: both instances are
    inside the sweep before either is allowed to reach its first statement. The contention is on
    the order's own row, and `SKIP LOCKED` is what turns the loser away instead of blocking it.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    order_id = await _ready_pickup_order(cod_app, seller, buyer, listing_body, order_body)
    backdate_ready(order_id)

    ready = asyncio.Barrier(2)

    async def sweep() -> int:
        await ready.wait()
        return await _sweep(cod_settings)

    first, second = await asyncio.gather(sweep(), sweep())

    assert first + second == 1
    row = _row(stored_orders(), order_id)
    assert row["status"] == "DELIVERED"
    assert row["delivered_via"] == "PICKUP_WINDOW"
    assert stored_reservations()[0]["state"] == "CONSUMED"


async def test_a_worker_racing_a_seller_confirms_once(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_ready: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """The clock and the seller are two writers of one handover, and one of them is turned away.

    The pickup is past its window, so the seller's code answers the same fact the sweep does --
    the confirmation is judged against the clock, not the stored status, and the answer is already
    delivered. The row may carry only one settlement, and exactly one road commits: the barrier
    holds both at the starting line, and whichever wins, the mechanism recorded is that road's and
    not both.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    order_id = await _ready_pickup_order(cod_app, seller, buyer, listing_body, order_body)
    code = await _pickup_code(cod_app, buyer, order_id)
    backdate_ready(order_id)

    start = asyncio.Barrier(2)

    async def sweep() -> int:
        await start.wait()
        return await _sweep(cod_settings)

    async def seller_confirms() -> httpx.Response:
        await start.wait()
        return await cod_app.post(
            f"/v1/orders/{order_id}/confirm-pickup",
            headers={**seller, "Idempotency-Key": str(uuid.uuid4())},
            json={"pickup_code": code},
        )

    confirmation, settled = await asyncio.gather(seller_confirms(), sweep())

    # One road commits, the other is turned away: the seller's code is refused when the sweep was
    # first, and the sweep finds nothing when the seller's confirmation was.
    assert settled + (1 if confirmation.status_code == 200 else 0) == 1
    row = _row(stored_orders(), order_id)
    assert row["status"] == "DELIVERED"
    assert row["delivered_via"] in {"PICKUP_WINDOW", "PICKUP_CODE"}
    assert stored_reservations()[0]["state"] == "CONSUMED"
