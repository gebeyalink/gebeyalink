"""FR-307: a seller is reminded before a listing with no offers expires.

The reminder is owed at forty-eight hours and again at twenty-four, and only while the listing
has no open offer. A listing whose only offer was rejected still gets the word: a rejected bid
is a negotiation that ended, not an offer a buyer can still act on. The worker writes a
notification row rather than sending directly, so the drain remains the one road a message
takes to a handset.

The tests drive the real application against PostgreSQL and read the notification rows straight
out of it. The listing is made through the API, so its expiry and seller are the rows the worker
will actually see rather than values assembled in the test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import psycopg.rows
import pytest
from fastapi import FastAPI

from apps.api import worker
from apps.api.core.config import Settings
from apps.api.main import create_app
from apps.api.providers.sms import RecordingSmsProvider

pytestmark = pytest.mark.integration

SELLER = "+251911000081"
BUYER = "+251911000082"


@pytest.fixture()
async def expiry_app(
    both_modes_settings: Settings,
    clean_database: None,
    clean_store: None,
    sms_recorder: RecordingSmsProvider,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, Settings]]:
    """The both-modes application and a client on this test's own event loop.

    Both funding modes are open so a test that places an offer does not trip over the mode flag
    instead of the rule it is proving. The lifespan binds the engine to this test's loop, which
    is what lets the request that creates the listing and the worker body that sweeps it share
    one engine rather than two.
    """
    application = create_app(both_modes_settings)
    async with application.router.lifespan_context(application):
        application.state.sms_provider = sms_recorder
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://expiry.test"
        ) as http:
            yield application, http, both_modes_settings


def _sync_url(postgres_url: str) -> str:
    return postgres_url.replace("postgresql+psycopg://", "postgresql://")


def _rows_for(postgres_url: str, phone_e164: str, template_id: str) -> list[dict[str, object]]:
    """Every notification of this template sent to this account, read from PostgreSQL."""
    with (
        psycopg.connect(_sync_url(postgres_url), row_factory=psycopg.rows.dict_row) as connection,
    ):
        return list(
            connection.execute(
                "SELECT n.id, n.user_id, n.channel, n.template_id, n.priority, n.language,"
                " n.rendered_text, n.status, n.dedup_key"
                " FROM notifications n JOIN users u ON u.id = n.user_id"
                " WHERE u.phone_e164 = %s AND n.template_id = %s"
                " ORDER BY n.id",
                (phone_e164, template_id),
            ).fetchall()
        )


async def _sign_in(
    http: httpx.AsyncClient,
    code_for: Callable[[str], str],
    settings: Settings,
    phone_e164: str,
    *,
    language: str = "en",
) -> dict[str, str]:
    requested = await http.post("/v1/auth/otp/request", json={"phone_e164": phone_e164})
    assert requested.status_code == 200, requested.text
    verified = await http.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": phone_e164,
            "code": code_for(phone_e164),
            "terms_version": settings.terms_version,
            "language": language,
        },
    )
    assert verified.status_code == 200, verified.text
    return {"Authorization": f"Bearer {verified.json()['access_token']}"}


async def _listing(
    http: httpx.AsyncClient,
    headers: dict[str, str],
    body: Callable[..., dict[str, object]],
    *,
    expires_at: datetime,
) -> str:
    posted = await http.post(
        "/v1/listings",
        headers=headers,
        json=body(expires_at=expires_at.isoformat()),
    )
    assert posted.status_code == 201, posted.text
    return str(posted.json()["id"])


async def _offer(
    http: httpx.AsyncClient,
    buyer: dict[str, str],
    listing_id: str,
    offer_body: Callable[..., dict[str, object]],
) -> str:
    placed = await http.post(
        f"/v1/listings/{listing_id}/offers",
        headers=buyer,
        json=offer_body(),
    )
    assert placed.status_code == 201, placed.text
    return str(placed.json()["id"])


async def test_a_listing_with_no_open_offer_is_reminded_at_forty_eight_hours(
    expiry_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A listing expiring in forty-eight hours queues one Q3 reminder under the 48-hour key."""
    _, http, settings = expiry_app
    seller = await _sign_in(http, code_for, settings, SELLER)

    now = datetime.now(UTC).replace(microsecond=0)
    listing_id = await _listing(
        http,
        seller,
        listing_body,
        expires_at=now + timedelta(hours=48),
    )

    assert await worker.run_expiry_reminder(settings, now=now) == 1

    rows = _rows_for(postgres_url, SELLER, "listing_expiring")
    assert len(rows) == 1
    row = rows[0]
    assert row["channel"] == "SMS"
    assert row["priority"] == 3
    assert row["status"] == "QUEUED"
    assert row["dedup_key"] == f"listing_expiring:{listing_id}:48"


async def test_a_listing_with_an_open_offer_is_not_reminded(
    expiry_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """An open offer is a buyer still at the table, so the seller is not warned."""
    _, http, settings = expiry_app
    seller = await _sign_in(http, code_for, settings, SELLER)
    buyer = await _sign_in(http, code_for, settings, BUYER, language="en")

    now = datetime.now(UTC).replace(microsecond=0)
    listing_id = await _listing(
        http,
        seller,
        listing_body,
        expires_at=now + timedelta(hours=48),
    )
    await _offer(http, buyer, listing_id, offer_body)

    assert await worker.run_expiry_reminder(settings, now=now) == 0
    assert _rows_for(postgres_url, SELLER, "listing_expiring") == []


async def test_a_listing_whose_only_offer_was_rejected_is_reminded(
    expiry_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """A rejected bid is not an open offer, so the listing still gets its reminder."""
    _, http, settings = expiry_app
    seller = await _sign_in(http, code_for, settings, SELLER)
    buyer = await _sign_in(http, code_for, settings, BUYER, language="en")

    now = datetime.now(UTC).replace(microsecond=0)
    listing_id = await _listing(
        http,
        seller,
        listing_body,
        expires_at=now + timedelta(hours=48),
    )
    offer_id = await _offer(http, buyer, listing_id, offer_body)
    rejected = await http.post(f"/v1/offers/{offer_id}/reject", headers=seller)
    assert rejected.status_code == 200, rejected.text

    assert await worker.run_expiry_reminder(settings, now=now) == 1
    rows = _rows_for(postgres_url, SELLER, "listing_expiring")
    assert len(rows) == 1
    assert rows[0]["dedup_key"] == f"listing_expiring:{listing_id}:48"


async def test_a_listing_outside_the_lead_window_waits_for_the_next_one(
    expiry_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A listing expiring in forty-seven hours is skipped at forty-eight.

    The twenty-four-hour sweep catches it when its own window comes.
    """
    _, http, settings = expiry_app
    seller = await _sign_in(http, code_for, settings, SELLER)

    base = datetime.now(UTC).replace(microsecond=0)
    expires_at = base + timedelta(hours=47)
    listing_id = await _listing(http, seller, listing_body, expires_at=expires_at)

    assert await worker.run_expiry_reminder(settings, now=base) == 0
    assert _rows_for(postgres_url, SELLER, "listing_expiring") == []

    assert await worker.run_expiry_reminder(settings, now=expires_at - timedelta(hours=24)) == 1
    rows = _rows_for(postgres_url, SELLER, "listing_expiring")
    assert len(rows) == 1
    assert rows[0]["dedup_key"] == f"listing_expiring:{listing_id}:24"


async def test_two_sweeps_five_minutes_apart_write_one_reminder(
    expiry_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """The dedup key holds even while the listing is still inside the same lead window."""
    _, http, settings = expiry_app
    seller = await _sign_in(http, code_for, settings, SELLER)

    base = datetime.now(UTC).replace(microsecond=0)
    listing_id = await _listing(
        http,
        seller,
        listing_body,
        expires_at=base + timedelta(hours=48),
    )

    assert await worker.run_expiry_reminder(settings, now=base) == 1
    assert await worker.run_expiry_reminder(settings, now=base + timedelta(minutes=5)) == 0

    rows = _rows_for(postgres_url, SELLER, "listing_expiring")
    assert len(rows) == 1
    assert rows[0]["dedup_key"] == f"listing_expiring:{listing_id}:48"


async def test_the_reminder_can_be_switched_off(
    expiry_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A deployment that turns the reminder off writes nothing and returns zero."""
    _, http, settings = expiry_app
    seller = await _sign_in(http, code_for, settings, SELLER)

    now = datetime.now(UTC).replace(microsecond=0)
    await _listing(
        http,
        seller,
        listing_body,
        expires_at=now + timedelta(hours=48),
    )
    disabled = settings.model_copy(update={"expiry_reminder_enabled": False})

    assert await worker.run_expiry_reminder(disabled, now=now) == 0
    assert _rows_for(postgres_url, SELLER, "listing_expiring") == []


async def test_the_reminder_is_written_in_the_sellers_language(
    expiry_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """An Oromo seller reads Oromo, and the row records the language its words are in."""
    _, http, settings = expiry_app
    seller = await _sign_in(http, code_for, settings, SELLER, language="om")

    now = datetime.now(UTC).replace(microsecond=0)
    listing_id = await _listing(
        http,
        seller,
        listing_body,
        expires_at=now + timedelta(hours=48),
    )

    assert await worker.run_expiry_reminder(settings, now=now) == 1
    rows = _rows_for(postgres_url, SELLER, "listing_expiring")
    assert len(rows) == 1
    assert rows[0]["language"] == "om"
    assert rows[0]["dedup_key"] == f"listing_expiring:{listing_id}:48"
    assert "Beeksisni" in str(rows[0]["rendered_text"])
