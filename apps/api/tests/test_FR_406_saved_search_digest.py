"""FR-406: saved searches, and the one daily digest they earn.

The API half is three routes under ``/v1/me/searches``: list, save, delete. The worker half
is one note per trader per EAT day, naming the searches that have listings newer than their
own window and summarising any beyond the five-line cap.

These tests are integration tests because the rules they prove live in PostgreSQL: the
ten-per-user lock, the §9.4 filter set against real listings, the unique dedup key, and the
window ``last_digest_at`` opens and closes. The worker is called in the same event loop as
the requests through the application's lifespan, the same shape the expiry-reminder tests
use.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import psycopg
import psycopg.rows
import pytest
from celery.schedules import crontab  # type: ignore[import-untyped]
from fastapi import FastAPI

from apps.api import worker
from apps.api.core.config import Settings
from apps.api.main import create_app
from apps.api.providers.sms import RecordingSmsProvider

pytestmark = pytest.mark.integration

BUYER = "+251911000091"
SELLER = "+251911000092"


@pytest.fixture()
async def saved_search_app(
    settings: Settings,
    clean_database: None,
    clean_store: None,
    sms_recorder: RecordingSmsProvider,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, Settings]]:
    """The application and a client on this test's own event loop.

    The lifespan binds the engine to this loop, which is what lets the request that saves a
    search and the worker body that reads it share one engine rather than two.
    """
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        application.state.sms_provider = sms_recorder
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://saved.test"
        ) as http:
            yield application, http, settings


def _sync_url(postgres_url: str) -> str:
    return postgres_url.replace("postgresql+psycopg://", "postgresql://")


def _rows_for(postgres_url: str, phone_e164: str) -> list[dict[str, object]]:
    """Every saved-search digest queued for this account, read from PostgreSQL."""
    with (
        psycopg.connect(_sync_url(postgres_url), row_factory=psycopg.rows.dict_row) as connection,
    ):
        return list(
            connection.execute(
                "SELECT n.id, n.user_id, n.channel, n.template_id, n.priority, n.language,"
                " n.rendered_text, n.status, n.dedup_key"
                " FROM notifications n JOIN users u ON u.id = n.user_id"
                " WHERE u.phone_e164 = %s AND n.template_id = 'saved_search_digest'"
                " ORDER BY n.created_at, n.id",
                (phone_e164,),
            ).fetchall()
        )


def _database_now(postgres_url: str) -> datetime:
    """The database's clock, which is the clock a listing's ``created_at`` comes from."""
    with psycopg.connect(_sync_url(postgres_url)) as connection:
        row = connection.execute("SELECT now()").fetchone()
        assert row is not None
        return cast(datetime, row[0])


def _saved_search_row(postgres_url: str, search_id: str) -> dict[str, object]:
    """The stored bookkeeping for one search: what the API does not publish."""
    with (
        psycopg.connect(_sync_url(postgres_url), row_factory=psycopg.rows.dict_row) as connection,
    ):
        row = connection.execute(
            "SELECT digest, digest_channel, last_digest_at FROM saved_searches WHERE id = %s",
            (search_id,),
        ).fetchone()
        assert row is not None
        return dict(row)


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


async def _save(
    http: httpx.AsyncClient, headers: dict[str, str], **filters: object
) -> dict[str, object]:
    body: dict[str, object] = {"digest": True, "digest_channel": "SMS", **filters}
    saved = await http.post("/v1/me/searches", headers=headers, json=body)
    assert saved.status_code == 201, saved.text
    return cast(dict[str, object], saved.json())


async def _listing(
    http: httpx.AsyncClient,
    headers: dict[str, str],
    body: Callable[..., dict[str, object]],
    **overrides: object,
) -> str:
    posted = await http.post("/v1/listings", headers=headers, json=body(**overrides))
    assert posted.status_code == 201, posted.text
    return str(posted.json()["id"])


def _eat_day(moment: datetime) -> str:
    """The same EAT day rule the worker uses, written out once for the assertions."""
    return (moment.astimezone(UTC) + timedelta(hours=3)).date().isoformat()


async def test_a_search_can_be_saved_listed_and_deleted(
    saved_search_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
) -> None:
    """The three routes, and a body that keeps the worker's bookkeeping private."""
    _, http, settings = saved_search_app
    buyer = await _sign_in(http, code_for, settings, BUYER)

    empty = await http.get("/v1/me/searches", headers=buyer)
    assert empty.status_code == 200
    assert empty.json() == []

    unknown = await http.post("/v1/me/searches", headers=buyer, json={"crop_id": 99_999})
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["error"]["details"] == {"fields": {"crop_id": "not_a_seeded_crop"}}

    unknown_region = await http.post("/v1/me/searches", headers=buyer, json={"region_code": "ZZ"})
    assert unknown_region.status_code == 422, unknown_region.text
    assert unknown_region.json()["error"]["details"] == {
        "fields": {"region_code": "not_a_seeded_region"}
    }

    unknown_woreda = await http.post("/v1/me/searches", headers=buyer, json={"woreda_id": 99_999})
    assert unknown_woreda.status_code == 422, unknown_woreda.text
    assert unknown_woreda.json()["error"]["details"] == {
        "fields": {"woreda_id": "not_a_seeded_woreda"}
    }

    saved = await _save(
        http,
        buyer,
        crop_id=1,
        region_code="OR",
        price_max=4_000_000,
        organic=False,
        digest=True,
        digest_channel="PUSH",
    )
    assert saved["crop_id"] == 1
    assert saved["region_code"] == "OR"
    assert saved["digest"] is True
    assert saved["digest_channel"] == "PUSH"
    assert "user_id" not in saved
    assert "last_digest_at" not in saved

    listed = await http.get("/v1/me/searches", headers=buyer)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [saved["id"]]

    search_id = str(saved["id"])
    deleted = await http.delete(f"/v1/me/searches/{search_id}", headers=buyer)
    assert deleted.status_code == 204
    again = await http.delete(f"/v1/me/searches/{search_id}", headers=buyer)
    assert again.status_code == 204

    final = await http.get("/v1/me/searches", headers=buyer)
    assert final.json() == []


async def test_the_eleventh_save_is_refused_at_the_contract_limit(
    saved_search_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
) -> None:
    """Ten rows land; the eleventh is 422 and names the limit rather than inventing a code."""
    _, http, settings = saved_search_app
    buyer = await _sign_in(http, code_for, settings, BUYER)

    for _ in range(10):
        await _save(http, buyer)

    refused = await http.post("/v1/me/searches", headers=buyer, json={})
    assert refused.status_code == 422, refused.text
    body = refused.json()["error"]
    assert body["code"] == "LIMIT_EXCEEDED"
    assert body["details"] == {"field": "saved_searches", "limit": 10}


async def test_a_radius_needs_a_point_the_trader_has_given(
    saved_search_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
) -> None:
    """A radius is a distance from somewhere; somewhere arrives through the profile."""
    _, http, settings = saved_search_app
    buyer = await _sign_in(http, code_for, settings, BUYER)

    refused = await http.post(
        "/v1/me/searches", headers=buyer, json={"radius_km": 10, "digest": True}
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["details"] == {"fields": {"radius_km": "requires_your_location"}}

    located = await http.patch(
        "/v1/me",
        headers=buyer,
        json={"gps": {"latitude": 8.541, "longitude": 39.268}},
    )
    assert located.status_code == 200, located.text

    allowed = await http.post(
        "/v1/me/searches", headers=buyer, json={"radius_km": 10, "digest": True}
    )
    assert allowed.status_code == 201, allowed.text


async def test_the_daily_digest_names_new_matches_and_advances_the_window(
    saved_search_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A PUSH search is sent on SMS, marked with the EAT dedup key, and its window moves.

    The search requests PUSH, which this deployment cannot carry. The queued row says SMS
    because that is what travels, and the fallback is logged rather than left to be inferred
    from a channel that disagrees with the saved request.
    """
    caplog.set_level(logging.INFO, logger="gebeyalink.worker")
    _, http, settings = saved_search_app
    buyer = await _sign_in(http, code_for, settings, BUYER)
    seller = await _sign_in(http, code_for, settings, SELLER)
    buyer_id = (await http.get("/v1/me", headers=buyer)).json()["id"]

    saved = await _save(
        http,
        buyer,
        crop_id=1,
        region_code="OR",
        digest=True,
        digest_channel="PUSH",
    )
    await _listing(http, seller, listing_body, crop_id=1, region_code="OR", woreda_id=1)

    now = _database_now(postgres_url)
    assert await worker.run_saved_search_digest(settings, now=now) == 1

    rows = _rows_for(postgres_url, BUYER)
    assert len(rows) == 1
    row = rows[0]
    assert row["channel"] == "SMS"
    assert row["priority"] == 3
    assert row["status"] == "QUEUED"
    assert row["language"] == "en"
    assert row["dedup_key"] == f"saved_search_digest:{buyer_id}:{_eat_day(now)}"
    assert "1 new listings from your searches:" in str(row["rendered_text"])
    assert "Teff (Oromia): 1" in str(row["rendered_text"])
    assert "saved search digest push fallback to SMS" in caplog.text

    stored = _saved_search_row(postgres_url, str(saved["id"]))
    assert stored["digest"] is True
    assert stored["digest_channel"] == "PUSH"
    assert stored["last_digest_at"] == now


async def test_a_quiet_day_writes_nothing(
    saved_search_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
) -> None:
    """No new matching listing means no message, not an empty one."""
    _, http, settings = saved_search_app
    buyer = await _sign_in(http, code_for, settings, BUYER)

    await _save(http, buyer, crop_id=1, digest=True)

    now = datetime.now(UTC).replace(microsecond=0)
    assert await worker.run_saved_search_digest(settings, now=now) == 0
    assert _rows_for(postgres_url, BUYER) == []


async def test_a_search_for_another_crop_does_not_make_a_digest(
    saved_search_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """The §9.4 filter set is applied to the digest, not just to the search endpoint."""
    _, http, settings = saved_search_app
    buyer = await _sign_in(http, code_for, settings, BUYER)
    seller = await _sign_in(http, code_for, settings, SELLER)

    await _save(http, buyer, crop_id=2, digest=True)
    await _listing(http, seller, listing_body, crop_id=1)

    now = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    assert await worker.run_saved_search_digest(settings, now=now) == 0
    assert _rows_for(postgres_url, BUYER) == []


async def test_a_search_that_asked_for_no_digest_is_silent(
    saved_search_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """``digest=false`` is a saved search for reading, not a subscription to a text."""
    _, http, settings = saved_search_app
    buyer = await _sign_in(http, code_for, settings, BUYER)
    seller = await _sign_in(http, code_for, settings, SELLER)

    await _save(http, buyer, crop_id=1, digest=False)
    await _listing(http, seller, listing_body, crop_id=1)

    now = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    assert await worker.run_saved_search_digest(settings, now=now) == 0
    assert _rows_for(postgres_url, BUYER) == []


async def test_two_runs_on_one_eat_day_queue_one_message(
    saved_search_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """The dedup key, not the window, is what stops a second same-day note.

    A second listing arrives after the first run, so the first run's window does not hide
    it. The second run matches it and loses the unique-key race; the row stands, and the
    search's window stays where the first run left it for tomorrow's attempt.
    """
    _, http, settings = saved_search_app
    buyer = await _sign_in(http, code_for, settings, BUYER)
    seller = await _sign_in(http, code_for, settings, SELLER)
    buyer_id = (await http.get("/v1/me", headers=buyer)).json()["id"]

    await _save(http, buyer, crop_id=1, digest=True)
    await _listing(http, seller, listing_body, crop_id=1)

    now = _database_now(postgres_url)
    assert await worker.run_saved_search_digest(settings, now=now) == 1

    await _listing(http, seller, listing_body, crop_id=1)
    # A second run at the same recorded moment is still the same EAT day by construction;
    # the second listing is newer than the window, so only the key can be what refuses it.
    assert await worker.run_saved_search_digest(settings, now=now) == 0

    rows = _rows_for(postgres_url, BUYER)
    assert len(rows) == 1
    assert rows[0]["dedup_key"] == f"saved_search_digest:{buyer_id}:{_eat_day(now)}"


async def test_the_next_day_reports_only_what_arrived_after_the_last_digest(
    saved_search_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """The first digest advances the window; the second does not repeat the first listing."""
    _, http, settings = saved_search_app
    buyer = await _sign_in(http, code_for, settings, BUYER)
    seller = await _sign_in(http, code_for, settings, SELLER)

    await _save(http, buyer, crop_id=1, digest=True)
    await _listing(http, seller, listing_body, crop_id=1)

    first_now = _database_now(postgres_url)
    assert await worker.run_saved_search_digest(settings, now=first_now) == 1
    assert len(_rows_for(postgres_url, BUYER)) == 1

    await _listing(http, seller, listing_body, crop_id=1)
    second_now = first_now + timedelta(days=1)
    assert await worker.run_saved_search_digest(settings, now=second_now) == 1

    rows = _rows_for(postgres_url, BUYER)
    assert len(rows) == 2
    assert "1 new listings from your searches:" in str(rows[-1]["rendered_text"])


async def test_a_search_with_no_new_matches_keeps_its_own_last_digest_at(
    saved_search_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A quiet search keeps its window, so the listing that arrives later is not lost.

    Yesterday only the teff search matched. Its window moved; the wheat search's did not.
    Today a wheat listing arrives, and the wheat search is the one that contributes a line
    while the teff search stays quiet.
    """
    _, http, settings = saved_search_app
    buyer = await _sign_in(http, code_for, settings, BUYER)
    seller = await _sign_in(http, code_for, settings, SELLER)

    teff = await _save(http, buyer, crop_id=1, region_code="OR", digest=True)
    wheat = await _save(http, buyer, crop_id=2, region_code="AM", digest=True)
    teff_id = str(teff["id"])
    wheat_id = str(wheat["id"])

    await _listing(
        http, seller, listing_body, crop_id=1, region_code="OR", unit_price_santim=3_250_000
    )
    yesterday = _database_now(postgres_url)
    assert await worker.run_saved_search_digest(settings, now=yesterday) == 1

    assert _saved_search_row(postgres_url, teff_id)["last_digest_at"] == yesterday
    assert _saved_search_row(postgres_url, wheat_id)["last_digest_at"] is None

    await _listing(
        http, seller, listing_body, crop_id=2, region_code="AM", unit_price_santim=3_250_000
    )
    today = yesterday + timedelta(days=1)
    assert await worker.run_saved_search_digest(settings, now=today) == 1

    rows = _rows_for(postgres_url, BUYER)
    assert len(rows) == 2
    assert "Teff (Oromia): 1" in str(rows[0]["rendered_text"])
    assert "Wheat (Amhara): 1" in str(rows[-1]["rendered_text"])
    assert "Teff (Oromia): 1" not in str(rows[-1]["rendered_text"])
    assert _saved_search_row(postgres_url, teff_id)["last_digest_at"] == yesterday
    assert _saved_search_row(postgres_url, wheat_id)["last_digest_at"] == today


async def test_the_digest_names_five_searches_and_summarises_the_rest(
    saved_search_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """Six matching searches become five lines, one closing line, and six marked rows."""
    _, http, settings = saved_search_app
    buyer = await _sign_in(http, code_for, settings, BUYER)
    seller = await _sign_in(http, code_for, settings, SELLER)

    search_ids: list[str] = []
    for price_min in range(1, 7):
        crop_id, region_code = (1, "OR") if price_min <= 3 else (2, "AM")
        saved = await _save(
            http,
            buyer,
            crop_id=crop_id,
            region_code=region_code,
            price_min=price_min,
            digest=True,
        )
        search_ids.append(str(saved["id"]))
    await _listing(
        http, seller, listing_body, crop_id=1, region_code="OR", unit_price_santim=3_250_000
    )
    await _listing(
        http, seller, listing_body, crop_id=2, region_code="AM", unit_price_santim=3_250_000
    )

    now = _database_now(postgres_url)
    assert await worker.run_saved_search_digest(settings, now=now) == 1

    rows = _rows_for(postgres_url, BUYER)
    assert len(rows) == 1
    text = str(rows[0]["rendered_text"])
    lines = text.splitlines()
    assert "6 new listings from your searches:" in lines[0]
    assert sum(line.endswith(": 1") for line in lines[1:]) == 5
    assert any("Teff (Oromia): 1" in line for line in lines[1:])
    assert any("Wheat (Amhara): 1" in line for line in lines[1:])
    assert lines[-1] == "and 1 more searches with new listings."

    for search_id in search_ids:
        assert _saved_search_row(postgres_url, search_id)["last_digest_at"] == now


async def test_the_digest_can_be_switched_off(
    saved_search_app: tuple[FastAPI, httpx.AsyncClient, Settings],
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """A deployment that turns the digest off writes nothing and returns zero."""
    _, http, settings = saved_search_app
    buyer = await _sign_in(http, code_for, settings, BUYER)
    seller = await _sign_in(http, code_for, settings, SELLER)

    await _save(http, buyer, crop_id=1, digest=True)
    await _listing(http, seller, listing_body, crop_id=1)

    disabled = settings.model_copy(update={"saved_search_digest_enabled": False})
    now = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    assert await worker.run_saved_search_digest(disabled, now=now) == 0
    assert _rows_for(postgres_url, BUYER) == []


def test_the_digest_is_scheduled_for_1800_eat(settings: Settings) -> None:
    """The beat entry is the calendar's 18:00, in the zone the worker configures."""
    app = worker.build_celery(settings)
    entry = app.conf.beat_schedule[worker.SAVED_SEARCH_DIGEST_TASK_NAME]
    assert entry["task"] == worker.SAVED_SEARCH_DIGEST_TASK_NAME
    assert entry["schedule"] == crontab(hour=18, minute=0)
    assert worker.SAVED_SEARCH_DIGEST_TASK_NAME in app.tasks
