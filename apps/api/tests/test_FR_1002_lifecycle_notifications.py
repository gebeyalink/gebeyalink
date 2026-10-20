"""FR-1002's order and offer lifecycle messages, written in the event's own transaction.

Five events owe a trader a word: an accepted offer tells the buyer, a rejected offer tells the
buyer, a ready pickup tells the buyer where to collect and with which code, a shipment tells the
buyer it is on its way, and a settled delivery tells whichever party did not act. Each producer
follows the N1b pattern the offer-received message established: ``NotificationRepository.add``
writes into the same session as the event and the event's own commit lands it, so the row and the
event are one commit and a message that cannot be written is an event that does not happen.

The recipient's language is their account's, the lane is the template's declared Q2, and the
dedup key names the event — so a replayed request writes one row rather than two, and the party
who did not act is the only one told. The whole file runs against the COD-enabled application,
because this is the pilot whose loop these messages serve; the escrow path is not exercised here
beyond the offer events, which are mode-neutral.

The application's lifespan runs inside each test's own event loop, for the reason the offer
notification's own tests give: the engine is a process-wide singleton bound to the loop that built
it, so the request that writes the event and any worker body must share one loop.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import psycopg
import psycopg.rows
import pytest
from fastapi import FastAPI

from apps.api.core.config import Settings
from apps.api.main import create_app
from apps.api.models.notification import Notification
from apps.api.providers.sms import RecordingSmsProvider
from apps.api.repositories.notification_repository import NotificationRepository

pytestmark = pytest.mark.integration

SELLER = "+251911000071"
BUYER = "+251911000072"

#: The offer figures. The price is what the buyer's bid carries, so the accepted message names it:
#: 3,300,000 santim renders as ``33000`` through ``format_etb`` — 33,000 birr a kilo, the asking
#: price a market actually quotes. The offer events are mode-neutral and are not code-bounded.
ASK_SANTIM = 3_250_000
OFFER_SANTIM = 3_300_000
LISTING_KG = 2_000
BUY_KG = 200

#: The COD figures. A cash-on-delivery order is bounded by the ten-thousand-birr ceiling, so a
#: test about the pilot's loop buys small: 4,000 santim a kilo (40 birr) for 200 kilos is 8,000
#: birr, comfortably inside the ceiling and a realistic pilot basket.
COD_ASK_SANTIM = 4_000
COD_KG = 200


@pytest.fixture()
async def board_app(
    both_modes_settings: Settings,
    clean_database: None,
    clean_store: None,
    sms_recorder: RecordingSmsProvider,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, Settings]]:
    """The both-modes application and a client on this test's own event loop.

    Both funding modes are open so one file can drive the COD pilot's loop and the escrow path
    the acceptance events are shared with. The lifespan is what builds the process's engine and
    Redis client and binds them to this loop, so the request that writes an event and the worker
    body that settles a lapsed window share one engine rather than two.
    """
    application = create_app(both_modes_settings)
    async with application.router.lifespan_context(application):
        application.state.sms_provider = sms_recorder
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://orders.test"
        ) as http:
            yield application, http, both_modes_settings


def _sync_url(postgres_url: str) -> str:
    return postgres_url.replace("postgresql+psycopg://", "postgresql://")


def _user_id(postgres_url: str, phone_e164: str) -> uuid.UUID:
    with psycopg.connect(_sync_url(postgres_url)) as connection:
        row = connection.execute(
            "SELECT id FROM users WHERE phone_e164 = %s", (phone_e164,)
        ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def _notifications(postgres_url: str) -> list[dict[str, Any]]:
    """Every notification row, read straight out of PostgreSQL.

    A fresh connection each time, so the answer is what has committed rather than what some
    still-open transaction holds: a producer that failed must leave nothing behind, and only the
    database can say that.
    """
    with psycopg.connect(_sync_url(postgres_url), row_factory=psycopg.rows.dict_row) as connection:
        return list(
            connection.execute(
                "SELECT id, user_id, channel, template_id, priority, language, rendered_text,"
                " status, dedup_key FROM notifications ORDER BY id"
            ).fetchall()
        )


def _rows_for(postgres_url: str, phone_e164: str, template_id: str) -> list[dict[str, Any]]:
    user_id = _user_id(postgres_url, phone_e164)
    return [
        row
        for row in _notifications(postgres_url)
        if row["user_id"] == user_id and row["template_id"] == template_id
    ]


async def _sign_in(
    http: httpx.AsyncClient,
    code_for: Callable[[str], str],
    settings: Settings,
    phone_e164: str,
    *,
    language: str | None = None,
) -> dict[str, str]:
    requested = await http.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})
    assert requested.status_code == 200, requested.text
    body: dict[str, object] = {
        "phone_e164": phone_e164,
        "code": code_for(phone_e164),
        "terms_version": settings.terms_version,
    }
    if language is not None:
        body["language"] = language
    verified = await http.post("/v1/auth/otp/verify", json=body)
    assert verified.status_code == 200, verified.text
    return {"Authorization": f"Bearer {verified.json()['access_token']}"}


async def _sign_in_pair(
    http: httpx.AsyncClient,
    code_for: Callable[[str], str],
    settings: Settings,
    *,
    seller_language: str = "en",
    buyer_language: str = "en",
) -> tuple[dict[str, str], dict[str, str]]:
    seller = await _sign_in(http, code_for, settings, SELLER, language=seller_language)
    buyer = await _sign_in(http, code_for, settings, BUYER, language=buyer_language)
    return seller, buyer


async def _listing(
    http: httpx.AsyncClient,
    headers: dict[str, str],
    body: Callable[..., dict[str, object]],
    *,
    delivery_options: list[str] | None = None,
    unit_price_santim: int = ASK_SANTIM,
    **overrides: object,
) -> str:
    posted = await http.post(
        "/v1/listings",
        headers=headers,
        json=body(
            quantity_kg=LISTING_KG,
            unit_price_santim=unit_price_santim,
            delivery_options=delivery_options or ["PICKUP"],
            **overrides,
        ),
    )
    assert posted.status_code == 201, posted.text
    return str(posted.json()["id"])


async def _offer(
    http: httpx.AsyncClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    *,
    payment_mode: str = "ESCROW",
) -> tuple[str, str]:
    """A listing and the offer the buyer places on it, and their identities."""
    listing_id = await _listing(http, seller, listing_body)
    placed = await http.post(
        f"/v1/listings/{listing_id}/offers",
        headers=buyer,
        json=offer_body(price_santim=OFFER_SANTIM, quantity_kg=BUY_KG, payment_mode=payment_mode),
    )
    assert placed.status_code == 201, placed.text
    return str(placed.json()["id"]), listing_id


async def _cod_order(
    http: httpx.AsyncClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    *,
    delivery_method: str = "PICKUP",
) -> tuple[str, str]:
    """A COD order bought at the asking price, and its listing's identity.

    COD rather than escrow because this file is about the pilot's loop; buy-now rather than an
    acceptance so an order exists without the offer machinery the offer tests already cover. The
    figures are small because a cash-on-delivery order is bounded by the ten-thousand-birr
    ceiling and a test about a message should not be defeated by the rule that caps the mode.
    """
    listing_id = await _listing(
        http,
        seller,
        listing_body,
        unit_price_santim=COD_ASK_SANTIM,
        delivery_options=[delivery_method],
        delivery_fee_santim=150_000 if delivery_method == "SELLER_DELIVERY" else 0,
    )
    bought = await http.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**buyer, "Idempotency-Key": str(uuid.uuid4())},
        json=order_body(quantity_kg=COD_KG, payment_mode="COD", delivery_method=delivery_method),
    )
    assert bought.status_code == 201, bought.text
    return str(bought.json()["id"]), listing_id


async def _confirm(http: httpx.AsyncClient, headers: dict[str, str], order_id: str) -> None:
    """A COD order's party signature; the second one confirms the order."""
    response = await http.post(f"/v1/orders/{order_id}/confirm", headers=headers)
    assert response.status_code == 200, response.text


async def _confirm_both(
    http: httpx.AsyncClient, seller: dict[str, str], buyer: dict[str, str], order_id: str
) -> None:
    await _confirm(http, seller, order_id)
    await _confirm(http, buyer, order_id)


async def _fulfill(
    http: httpx.AsyncClient, headers: dict[str, str], order_id: str, step: str
) -> httpx.Response:
    return await http.post(
        f"/v1/orders/{order_id}/{step}",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )


async def test_an_accepted_offer_tells_the_buyer(
    board_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
) -> None:
    """The seller accepting a bid writes the buyer one Q2 message, in the buyer's language.

    The row is read from PostgreSQL rather than from the reply, so the assertion is the database's
    account of what was queued. The bid was the buyer's and the acceptance is the seller's answer,
    so the buyer is the recipient and nobody else is written to.
    """
    _, http, settings = board_app
    seller, buyer = await _sign_in_pair(
        http, code_for, settings, seller_language="en", buyer_language="am"
    )
    offer_id, _ = await _offer(http, seller, buyer, listing_body, offer_body)

    accepted = await http.post(
        f"/v1/offers/{offer_id}/accept",
        headers={**seller, "Idempotency-Key": str(uuid.uuid4())},
        json=order_body(),
    )
    assert accepted.status_code == 201, accepted.text

    rows = _rows_for(postgres_url, BUYER, "offer_accepted")
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == _user_id(postgres_url, BUYER)
    assert row["channel"] == "SMS"
    assert row["priority"] == 2
    assert row["language"] == "am"
    assert row["status"] == "QUEUED"
    assert row["dedup_key"] == f"offer_accepted:{offer_id}"

    # Nobody told the seller, and the seller is the one who acted.
    assert _rows_for(postgres_url, SELLER, "offer_accepted") == []


async def test_a_rejected_offer_tells_the_buyer(
    board_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """The seller refusing a bid writes the buyer one Q2 message, and names no other recipient."""
    _, http, settings = board_app
    seller, buyer = await _sign_in_pair(
        http, code_for, settings, seller_language="am", buyer_language="om"
    )
    offer_id, _ = await _offer(http, seller, buyer, listing_body, offer_body)

    rejected = await http.post(f"/v1/offers/{offer_id}/reject", headers=seller)
    assert rejected.status_code == 200, rejected.text

    rows = _rows_for(postgres_url, BUYER, "offer_rejected")
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == _user_id(postgres_url, BUYER)
    assert row["priority"] == 2
    assert row["language"] == "om"
    assert row["dedup_key"] == f"offer_rejected:{offer_id}"
    assert _rows_for(postgres_url, SELLER, "offer_rejected") == []


async def test_a_ready_pickup_tells_the_buyer_with_the_code(
    board_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
) -> None:
    """A ready pickup writes the buyer the code-carrying template, and only the buyer.

    The template is `order_ready_for_pickup_code`, authored for this event because the plainer
    template names no code and FR-1002's ready message is the one that carries it. The rendered
    text holds the six-digit code the buyer presents at the counter.
    """
    _, http, settings = board_app
    seller, buyer = await _sign_in_pair(http, code_for, settings, buyer_language="en")
    order_id, _ = await _cod_order(http, seller, buyer, listing_body, order_body)
    await _confirm_both(http, seller, buyer, order_id)

    marked = await _fulfill(http, seller, order_id, "ready")
    assert marked.status_code == 200, marked.text
    # The seller's own copy of the order does not carry the code they are meant to ask for; it is
    # the buyer's, so the buyer reads it back.
    read = await http.get(f"/v1/orders/{order_id}", headers=buyer)
    assert read.status_code == 200, read.text
    code = read.json()["pickup_code"]
    assert code is not None

    rows = _rows_for(postgres_url, BUYER, "order_ready_for_pickup_code")
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == _user_id(postgres_url, BUYER)
    assert row["priority"] == 2
    assert row["dedup_key"] == f"order_ready:{order_id}"
    assert str(code) in row["rendered_text"]
    assert _rows_for(postgres_url, SELLER, "order_ready_for_pickup_code") == []


async def test_a_shipment_tells_the_buyer(
    board_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
) -> None:
    """A shipment writes the buyer one Q2 message and no other party."""
    _, http, settings = board_app
    seller, buyer = await _sign_in_pair(http, code_for, settings, buyer_language="am")
    order_id, _ = await _cod_order(
        http, seller, buyer, listing_body, order_body, delivery_method="SELLER_DELIVERY"
    )
    await _confirm_both(http, seller, buyer, order_id)

    marked = await _fulfill(http, seller, order_id, "shipped")
    assert marked.status_code == 200, marked.text

    rows = _rows_for(postgres_url, BUYER, "order_shipped")
    assert len(rows) == 1
    assert rows[0]["user_id"] == _user_id(postgres_url, BUYER)
    assert rows[0]["priority"] == 2
    assert rows[0]["language"] == "am"
    assert rows[0]["dedup_key"] == f"order_shipped:{order_id}"
    assert _rows_for(postgres_url, SELLER, "order_shipped") == []


async def test_a_pickup_code_settlement_tells_the_buyer(
    board_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
) -> None:
    """`PICKUP_CODE` settlement: the seller acted at the counter, so the buyer is told."""
    _, http, settings = board_app
    seller, buyer = await _sign_in_pair(http, code_for, settings, buyer_language="en")
    order_id, _ = await _cod_order(http, seller, buyer, listing_body, order_body)
    await _confirm_both(http, seller, buyer, order_id)
    marked = await _fulfill(http, seller, order_id, "ready")
    assert marked.status_code == 200, marked.text
    read = await http.get(f"/v1/orders/{order_id}", headers=buyer)
    assert read.status_code == 200, read.text
    code = read.json()["pickup_code"]

    settled = await http.post(
        f"/v1/orders/{order_id}/confirm-pickup",
        headers={**seller, "Idempotency-Key": str(uuid.uuid4())},
        json={"pickup_code": code},
    )
    assert settled.status_code == 200, settled.text

    assert len(_rows_for(postgres_url, BUYER, "order_delivered")) == 1
    assert _rows_for(postgres_url, SELLER, "order_delivered") == []


async def test_a_buyer_confirmed_settlement_tells_the_seller(
    board_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
) -> None:
    """`BUYER_CONFIRMED` settlement: the buyer acted, so the seller is told."""
    _, http, settings = board_app
    seller, buyer = await _sign_in_pair(http, code_for, settings, buyer_language="en")
    order_id, _ = await _cod_order(
        http, seller, buyer, listing_body, order_body, delivery_method="SELLER_DELIVERY"
    )
    await _confirm_both(http, seller, buyer, order_id)
    await _fulfill(http, seller, order_id, "shipped")

    settled = await _fulfill(http, buyer, order_id, "confirm-delivery")
    assert settled.status_code == 200, settled.text

    assert len(_rows_for(postgres_url, SELLER, "order_delivered")) == 1
    assert _rows_for(postgres_url, BUYER, "order_delivered") == []


async def test_an_auto_settled_settlement_tells_both_parties(
    board_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    backdate_shipped: Callable[[str], None],
) -> None:
    """A window that closed on its own settled a handover nobody witnessed: both are told.

    The shipment window is moved into the past behind the API's back, exactly as the clock would
    have, and the worker sweep settles it. Both parties get a row, and the keys are per recipient.
    """
    from apps.api import worker

    _, http, settings = board_app
    seller, buyer = await _sign_in_pair(http, code_for, settings)
    order_id, _ = await _cod_order(
        http, seller, buyer, listing_body, order_body, delivery_method="SELLER_DELIVERY"
    )
    await _confirm_both(http, seller, buyer, order_id)
    await _fulfill(http, seller, order_id, "shipped")
    backdate_shipped(order_id)

    settled = await worker.run_shipment_sweep(settings)
    assert settled == 1

    buyer_rows = _rows_for(postgres_url, BUYER, "order_delivered")
    seller_rows = _rows_for(postgres_url, SELLER, "order_delivered")
    assert len(buyer_rows) == 1
    assert len(seller_rows) == 1
    assert (
        buyer_rows[0]["dedup_key"] == f"order_delivered:{order_id}:{_user_id(postgres_url, BUYER)}"
    )
    assert (
        seller_rows[0]["dedup_key"]
        == f"order_delivered:{order_id}:{_user_id(postgres_url, SELLER)}"
    )


async def test_a_replayed_lifecycle_event_writes_one_row(
    board_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """The dedup key holds: a refused bid refused again under a new key still writes one message.

    The request is refused once, then the same refusal is attempted again — the guard now refuses
    the second outright, but the point is that even a second write of the same event's key cannot
    land a second row. The key is read back to show the event's identity is the bid's.
    """
    _, http, settings = board_app
    seller, buyer = await _sign_in_pair(http, code_for, settings)
    offer_id, _ = await _offer(http, seller, buyer, listing_body, offer_body)

    first = await http.post(f"/v1/offers/{offer_id}/reject", headers=seller)
    assert first.status_code == 200, first.text
    again = await http.post(f"/v1/offers/{offer_id}/reject", headers=seller)
    assert again.status_code in (200, 409), again.text

    rows = _rows_for(postgres_url, BUYER, "offer_rejected")
    assert len(rows) == 1
    assert rows[0]["dedup_key"] == f"offer_rejected:{offer_id}"


async def test_a_notification_that_cannot_be_written_leaves_no_event_behind(
    board_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break the message write and the event does not happen: the order stays where it was.

    A ready pickup whose message cannot be written is not a pickup the platform may stand: the
    mark fails and the order's status is unchanged, because the message and the transition were
    one commit.
    """
    _, http, settings = board_app
    seller, buyer = await _sign_in_pair(http, code_for, settings)
    order_id, _ = await _cod_order(http, seller, buyer, listing_body, order_body)
    await _confirm_both(http, seller, buyer, order_id)

    async def _refuse_to_queue(self: NotificationRepository, notification: Notification) -> Any:
        raise RuntimeError("the notification write is broken on purpose")

    monkeypatch.setattr(NotificationRepository, "add", _refuse_to_queue)

    with pytest.raises(RuntimeError, match="broken on purpose"):
        await _fulfill(http, seller, order_id, "ready")

    monkeypatch.undo()
    read = await http.get(f"/v1/orders/{order_id}", headers=buyer)
    assert read.status_code == 200, read.text
    assert read.json()["status"] == "CONFIRMED"
    assert _rows_for(postgres_url, BUYER, "order_ready_for_pickup_code") == []


async def test_the_escrow_offer_events_are_mode_neutral(
    board_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
) -> None:
    """An escrow bid's acceptance writes the same buyer message a COD one does.

    The offer templates name neither funding nor escrow, so no COD-specific variant exists for
    them and none is needed: the acceptance message is the same sentence in either mode, and this
    pins that the escrow path writes it too.
    """
    _, http, settings = board_app
    seller, buyer = await _sign_in_pair(http, code_for, settings)
    offer_id, _ = await _offer(http, seller, buyer, listing_body, offer_body, payment_mode="ESCROW")

    accepted = await http.post(
        f"/v1/offers/{offer_id}/accept",
        headers={**seller, "Idempotency-Key": str(uuid.uuid4())},
        json=order_body(),
    )
    assert accepted.status_code == 201, accepted.text

    rows = _rows_for(postgres_url, BUYER, "offer_accepted")
    assert len(rows) == 1
    assert rows[0]["dedup_key"] == f"offer_accepted:{offer_id}"
