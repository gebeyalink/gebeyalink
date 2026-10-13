"""The worker settling a lapsed shipment, rather than the reader who used to.

A shipment whose crop's holding window has closed is deemed received. That judgement is the
clock's whether or not anything has got round to writing it down, and this file is about the half
of the arrangement that writes: a worker on a cadence, rather than whichever reader happened to
look. The read still derives the same status it always did — the difference the migration makes is
*when* the row and the reservation are persisted, not what a user sees.

Two things are proved here and nothing else. First, the settlement itself: the same transition, the
same consumption, the same row left alone if the crop offers no window, and one bounded batch per
run. Second, the hard part of moving it — two processes now race over the same orders, and exactly
one settlement must come out of every race, whether the second contestant is another worker or the
buyer's own confirmation.
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
from apps.api.services.order_service import OrderService, shipment_window_closed

# SPEC A20: this file drives the real app against an ephemeral Postgres, so it is an
# integration-layer test.
pytestmark = pytest.mark.integration

#: The terms this file's accounts accept. The API gives the value no meaning; it is required
#: because a handset must send one, and a body without it is a different test.
TERMS_VERSION = "2026-01-01"

SELLER_PHONE = "+251911000001"
BUYER_PHONE = "+251911000003"

CHEAP_LISTING_KG = 2_000
CHEAP_ASK_SANTIM = 1_000
CHEAP_DELIVERY_FEE_SANTIM = 150_000
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
    """One run of the worker's shipment sweep, awaited by the caller."""
    return worker.run_shipment_sweep(settings)


def _sync_url(postgres_url: str) -> str:
    return postgres_url.replace("postgresql+psycopg://", "postgresql://")


def _set_role(postgres_url: str, phone: str, role: str) -> None:
    with psycopg.connect(_sync_url(postgres_url), autocommit=True) as connection:
        connection.execute("UPDATE users SET role = %s WHERE phone_e164 = %s", (role, phone))


def _set_shipped_at(postgres_url: str, order_id: str, moment: datetime) -> None:
    """Write the one column the shipment window is measured from, to the second."""
    with psycopg.connect(_sync_url(postgres_url), autocommit=True) as connection:
        connection.execute("UPDATE orders SET shipped_at = %s WHERE id = %s", (moment, order_id))


def _hold_hours(postgres_url: str) -> int:
    """The holding window of the crop this file's listings name."""
    with psycopg.connect(_sync_url(postgres_url)) as connection:
        row = connection.execute("SELECT escrow_hold_hours FROM crops WHERE id = 1").fetchone()
    assert row is not None
    return int(row[0])


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


async def _shipped_order(
    http: httpx.AsyncClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    *,
    delivery_options: list[str] | None = None,
    delivery_method: str = "SELLER_DELIVERY",
    delivery_fee_santim: int = CHEAP_DELIVERY_FEE_SANTIM,
) -> str:
    """Stand up a COD order of the given method, walk it to shipped, behind no clock."""
    posted = await http.post(
        "/v1/listings",
        headers=seller,
        json=listing_body(
            quantity_kg=CHEAP_LISTING_KG,
            unit_price_santim=CHEAP_ASK_SANTIM,
            delivery_options=delivery_options or ["SELLER_DELIVERY"],
            delivery_fee_santim=delivery_fee_santim,
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
            delivery_method=delivery_method,
        ),
    )
    assert bought.status_code == 201, bought.text
    order_id = str(bought.json()["id"])

    for confirmer in (buyer, seller):
        confirmed = await http.post(f"/v1/orders/{order_id}/confirm", headers=confirmer)
        assert confirmed.status_code == 200, confirmed.text

    shipped = await http.post(
        f"/v1/orders/{order_id}/shipped",
        headers={**seller, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert shipped.status_code == 200, shipped.text
    return order_id


def _row(orders: list[dict[str, object]], order_id: str) -> dict[str, object]:
    return next(order for order in orders if str(order["id"]) == order_id)


async def test_the_worker_settles_a_lapsed_shipment(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_shipped: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """The worker writes what the read derives: delivered, by the clock, and consumed.

    The status, the stamp and the reservation state are all read back from the database, because
    the point of moving the write to a worker is that the row is now the record and not merely
    the answer to whoever asked.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    order_id = await _shipped_order(cod_app, seller, buyer, listing_body, order_body)
    backdate_shipped(order_id)

    assert await _sweep(cod_settings) == 1

    row = _row(stored_orders(), order_id)
    assert row["status"] == "DELIVERED"
    assert row["delivered_via"] == "SHIPMENT_WINDOW"
    assert row["delivered_at"] is not None
    assert stored_reservations()[0]["state"] == "CONSUMED"


async def test_the_worker_settles_a_transport_partner_shipment(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_shipped: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """A buyer-arranged shipment is the same road to the sweep as a seller's.

    The window keys on the order's status and the crop's hours, never on who carried the crop, so
    the transport-partner order is settled identically and records the same mechanism.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    order_id = await _shipped_order(
        cod_app,
        seller,
        buyer,
        listing_body,
        order_body,
        delivery_options=["TRANSPORT_PARTNER"],
        delivery_method="TRANSPORT_PARTNER",
        delivery_fee_santim=0,
    )
    backdate_shipped(order_id)

    assert await _sweep(cod_settings) == 1

    row = _row(stored_orders(), order_id)
    assert row["status"] == "DELIVERED"
    assert row["delivered_via"] == "SHIPMENT_WINDOW"
    assert stored_reservations()[0]["state"] == "CONSUMED"


async def test_the_worker_settles_nothing_twice(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_shipped: Callable[[str], None],
) -> None:
    """A second run finds nothing: the order is no longer shipped to be selected."""
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    order_id = await _shipped_order(cod_app, seller, buyer, listing_body, order_body)
    backdate_shipped(order_id)

    assert await _sweep(cod_settings) == 1
    assert await _sweep(cod_settings) == 0


async def test_the_worker_leaves_alone_an_order_whose_crop_offers_no_window(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_shipped: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An order the crop lookup cannot answer for is left exactly where it is.

    The batched lookup is what remembers the crop's window, and a lookup that returns no value
    for an order means the sweep does not know how long this shipment may wait. Not knowing is
    not the same as it having passed, so the row is passed over rather than guessed at.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    order_id = await _shipped_order(cod_app, seller, buyer, listing_body, order_body)
    backdate_shipped(order_id)

    async def _no_window(self: OrderRepository, order_ids: list[uuid.UUID]) -> dict[Any, Any]:
        return {}

    monkeypatch.setattr(OrderRepository, "crop_hold_hours_for_orders", _no_window)

    assert await _sweep(cod_settings) == 0
    row = _row(stored_orders(), order_id)
    assert row["status"] == "SHIPPED"
    assert row["delivered_via"] is None


async def test_the_worker_settles_at_most_one_bounded_batch(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_shipped: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hundred is a promise about one run, and this is that promise made small.

    Three shipments have lapsed and the run is allowed two, so exactly two go and one waits for
    the next cadence. The bound is what keeps the worker's first statement a short one.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    first = await _shipped_order(cod_app, seller, buyer, listing_body, order_body)
    second = await _shipped_order(cod_app, seller, buyer, listing_body, order_body)
    third = await _shipped_order(cod_app, seller, buyer, listing_body, order_body)
    for order_id in (first, second, third):
        backdate_shipped(order_id)

    monkeypatch.setattr("apps.api.services.order_service.SHIPMENT_SWEEP_BATCH_SIZE", 2)

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
    backdate_shipped: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One broken order does not cost the others their settlement.

    The sweep writes one order per transaction, so a raise is that order's transaction rolled
    back and nothing more: the next order keeps its turn with a session that is clean again.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    broken = await _shipped_order(cod_app, seller, buyer, listing_body, order_body)
    intact = await _shipped_order(cod_app, seller, buyer, listing_body, order_body)
    backdate_shipped(broken)
    backdate_shipped(intact)

    settled = OrderService._settle_delivery

    async def _flaky(self: OrderService, order: Any, *args: Any, **kwargs: Any) -> Any:
        if str(order.id) == broken:
            raise RuntimeError("this settlement is broken on purpose")
        return await settled(self, order, *args, **kwargs)

    monkeypatch.setattr(OrderService, "_settle_delivery", _flaky)

    assert await _sweep(cod_settings) == 1

    assert _row(stored_orders(), broken)["status"] == "SHIPPED"
    assert _row(stored_orders(), intact)["status"] == "DELIVERED"


async def test_two_workers_racing_settle_once(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_shipped: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """Two sweeps over one lapsed shipment settle it once between them.

    The barrier is what makes this a race rather than two calls in a row: both instances are
    inside the sweep before either is allowed to reach its first statement. The contention is on
    the order's own row, and `SKIP LOCKED` is what turns the loser away instead of blocking it.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    order_id = await _shipped_order(cod_app, seller, buyer, listing_body, order_body)
    backdate_shipped(order_id)

    ready = asyncio.Barrier(2)

    async def sweep() -> int:
        await ready.wait()
        return await _sweep(cod_settings)

    first, second = await asyncio.gather(sweep(), sweep())

    assert first + second == 1
    row = _row(stored_orders(), order_id)
    assert row["status"] == "DELIVERED"
    assert row["delivered_via"] == "SHIPMENT_WINDOW"
    assert stored_reservations()[0]["state"] == "CONSUMED"


async def test_a_worker_racing_a_buyer_confirms_once(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_shipped: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """The clock and the buyer are two writers of one delivery, and one of them is turned away.

    The shipment is past its window, so the buyer's confirmation answers the same fact the sweep
    does — it is judged against the clock, not the stored status, and the answer is already
    delivered. The row may carry only one settlement, and that is what is asserted: the buyer is
    refused, the worker's road is the one that committed.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    order_id = await _shipped_order(cod_app, seller, buyer, listing_body, order_body)
    backdate_shipped(order_id)

    async def buyer_confirms() -> httpx.Response:
        return await cod_app.post(
            f"/v1/orders/{order_id}/confirm-delivery",
            headers={**buyer, "Idempotency-Key": str(uuid.uuid4())},
        )

    confirmation, settled = await asyncio.gather(buyer_confirms(), _sweep(cod_settings))

    assert settled == 1
    assert confirmation.status_code == 409, confirmation.text
    row = _row(stored_orders(), order_id)
    assert row["status"] == "DELIVERED"
    assert row["delivered_via"] == "SHIPMENT_WINDOW"
    assert stored_reservations()[0]["state"] == "CONSUMED"


async def test_the_worker_leaves_an_order_inside_its_window_alone(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_reservations: Callable[[], list[dict[str, object]]],
) -> None:
    """Inside the window the worker settles nothing, and the buyer's word still owns the ending."""
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    order_id = await _shipped_order(cod_app, seller, buyer, listing_body, order_body)

    assert await _sweep(cod_settings) == 0

    confirmed = await cod_app.post(
        f"/v1/orders/{order_id}/confirm-delivery",
        headers={**buyer, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert confirmed.status_code == 200, confirmed.text
    row = _row(stored_orders(), order_id)
    assert row["delivered_via"] == "BUYER_CONFIRMED"
    assert stored_reservations()[0]["state"] == "CONSUMED"


async def test_the_worker_does_not_settle_an_order_the_buyer_already_confirmed(
    cod_app: httpx.AsyncClient,
    cod_settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_shipped: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """A buyer's confirmation finishes the order, and a later sweep has nothing to add."""
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    order_id = await _shipped_order(cod_app, seller, buyer, listing_body, order_body)

    confirmed = await cod_app.post(
        f"/v1/orders/{order_id}/confirm-delivery",
        headers={**buyer, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert confirmed.status_code == 200, confirmed.text
    backdate_shipped(order_id)

    assert await _sweep(cod_settings) == 0
    assert _row(stored_orders(), order_id)["delivered_via"] == "BUYER_CONFIRMED"


async def test_a_lapsed_shipment_refuses_every_fulfillment_move(
    cod_app: httpx.AsyncClient,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_shipped: Callable[[str], None],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """With the window closed and the worker not yet round, the stored row is still shipped.

    Every route that could write a fulfilment reads the status the clock gives it before it acts,
    so a lapsed order that has not been persisted is refused by all of them — and refusing must
    leave the row exactly as it was.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    order_id = await _shipped_order(cod_app, seller, buyer, listing_body, order_body)
    backdate_shipped(order_id)

    refusals = [
        await cod_app.post(
            f"/v1/orders/{order_id}/confirm-delivery",
            headers={**buyer, "Idempotency-Key": str(uuid.uuid4())},
        ),
        await cod_app.post(
            f"/v1/orders/{order_id}/shipped",
            headers={**seller, "Idempotency-Key": str(uuid.uuid4())},
        ),
        await cod_app.post(
            f"/v1/orders/{order_id}/ready",
            headers={**seller, "Idempotency-Key": str(uuid.uuid4())},
        ),
        await cod_app.post(
            f"/v1/orders/{order_id}/cancel",
            headers={**buyer, "Idempotency-Key": str(uuid.uuid4())},
        ),
    ]

    for response in refusals:
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "ILLEGAL_STATE_TRANSITION"

    row = _row(stored_orders(), order_id)
    assert row["status"] == "SHIPPED"
    assert row["delivered_at"] is None


async def test_the_worker_s_boundary_is_the_read_path_s_boundary(
    cod_app: httpx.AsyncClient,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
) -> None:
    """The sweep's SQL and the read's Python answer the same question the same way.

    The window has two expressions in this codebase — `shipment_window_closed` for every reader
    and the repository's query for the worker — and a migration that left them disagreeing by a
    second would settle a shipment the read still calls shipped, or miss one it calls delivered.
    Three shipments are placed one second inside, exactly on, and one second outside the boundary,
    and each answer is compared across both expressions.
    """
    seller, buyer = await _parties(cod_app, code_for, postgres_url)
    hold_hours = _hold_hours(postgres_url)

    now = datetime.now(UTC)
    inside = now - timedelta(hours=hold_hours) + timedelta(seconds=1)
    on = now - timedelta(hours=hold_hours)
    outside = now - timedelta(hours=hold_hours) - timedelta(seconds=1)

    for shipped_at, expected in ((inside, False), (on, True), (outside, True)):
        order_id = await _shipped_order(cod_app, seller, buyer, listing_body, order_body)
        _set_shipped_at(postgres_url, order_id, shipped_at)

        async with get_session_factory()() as session:
            orders = OrderRepository(session)
            lapsed = await orders.list_lapsed_shipment_ids(now, limit=100)
            selected = order_id in {str(candidate) for candidate in lapsed}
            stored = await orders.find_by_id(uuid.UUID(order_id))
        assert stored is not None
        derived = shipment_window_closed(stored, now, hold_hours)

        assert derived is expected
        assert selected is expected
