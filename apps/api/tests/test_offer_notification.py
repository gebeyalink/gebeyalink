"""The offer event: the message a bid owes the seller, written with the bid.

Placing a bid is one transaction with two rows in it — the offer, and the message the seller
is owed about it. This file is that coupling: the message is written on the same session as
the offer and committed by the same commit, so a message that cannot be written is not an
offer that stands announced to nobody. The rest is what the drain does with the row once it
exists: carries it, holds it through the quiet window, and logs the seller masked.

The application's lifespan runs inside each test's own event loop, for the reason the drain's
own tests give: the engine is a process-wide singleton bound to the loop that built it, so the
request that places the offer and the drain that sends it must share one loop.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import psycopg.rows
import pytest
from fastapi import FastAPI

from apps.api import worker
from apps.api.core.config import Settings
from apps.api.main import create_app
from apps.api.models.notification import Notification
from apps.api.providers.sms import (
    RecordingMessagingProvider,
    RecordingSmsProvider,
    mask_phone,
)
from apps.api.repositories.crop_repository import CropRepository
from apps.api.repositories.notification_repository import NotificationRepository
from apps.api.repositories.user_repository import UserRepository
from packages.shared.messaging.queues import EAT_ZONE, SmsPriority, priority_rank

pytestmark = pytest.mark.integration

SELLER = "+251911000061"
BUYER = "+251911000062"

#: Noon UTC, which is 15:00 EAT: comfortably outside the quiet window, for the tests that are
#: not about the window.
NOON_EAT_UTC = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

#: What the seller reads when they read English and the crop is the catalogue's first, Teff.
#: The price is the offer fixture's 3,300,000 santim, which `format_etb` renders without a
#: trailing `.0`.
EXPECTED_ENGLISH = "You received an offer for Teff at 33000 birr/kg"


@pytest.fixture()
async def offer_app(
    settings: Settings,
    clean_database: None,
    clean_store: None,
    sms_recorder: RecordingSmsProvider,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    """The application and an HTTP client on this test's own event loop.

    The lifespan is what builds the process's engine and Redis client and binds them to this
    loop; the drain is awaited from the test body and shares that engine, so the request that
    places the offer and the drain that sends it run on one loop rather than two.
    """
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        application.state.sms_provider = sms_recorder
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://offers.test"
        ) as http:
            yield application, http


def _sync_url(postgres_url: str) -> str:
    return postgres_url.replace("postgresql+psycopg://", "postgresql://")


def _user_id(postgres_url: str, phone_e164: str) -> uuid.UUID:
    with psycopg.connect(_sync_url(postgres_url)) as connection:
        row = connection.execute(
            "SELECT id FROM users WHERE phone_e164 = %s", (phone_e164,)
        ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def _offers(postgres_url: str) -> list[dict[str, Any]]:
    """Every offer row, read straight out of PostgreSQL.

    A fresh connection every time, so the answer is what has been committed rather than what
    some still-open transaction is holding. That is the whole point of reading it here: a
    placement that failed must leave nothing behind, and only the database can say that.
    """
    with psycopg.connect(_sync_url(postgres_url), row_factory=psycopg.rows.dict_row) as connection:
        return list(
            connection.execute(
                "SELECT id, listing_id, buyer_id, status FROM offers ORDER BY id"
            ).fetchall()
        )


def _notifications(postgres_url: str) -> list[dict[str, Any]]:
    with psycopg.connect(_sync_url(postgres_url), row_factory=psycopg.rows.dict_row) as connection:
        return list(
            connection.execute(
                "SELECT id, user_id, channel, template_id, priority, language, rendered_text,"
                " status, dedup_key, attempt_count, next_attempt_at, sent_at"
                " FROM notifications ORDER BY id"
            ).fetchall()
        )


def _delivered_bodies(sms_recorder: RecordingSmsProvider, phone_e164: str) -> list[str]:
    return [message.body for message in sms_recorder.messages_to(phone_e164)]


async def _sign_in(
    http: httpx.AsyncClient,
    code_for: Callable[[str], str],
    settings: Settings,
    phone_e164: str,
    *,
    language: str | None = None,
) -> dict[str, str]:
    """Sign one trader in over this test's asynchronous client, in the language asked for."""
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


async def _place_offer(
    http: httpx.AsyncClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> str:
    """A listing under this buyer's seller, and the bid this buyer places on it."""
    posted = await http.post(
        "/v1/listings", headers=seller, json=listing_body(delivery_options=["PICKUP"])
    )
    assert posted.status_code == 201, posted.text
    listing_id = str(posted.json()["id"])
    placed = await http.post(f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body())
    assert placed.status_code == 201, placed.text
    return str(placed.json()["id"])


async def _drain(settings: Settings, sms_recorder: RecordingSmsProvider, *, now: datetime) -> Any:
    return await worker.run_notification_drain(
        settings, now=now, provider=RecordingMessagingProvider(recorder=sms_recorder)
    )


class _LanguageWithNoVariant:
    """A language the registry carries no variant for and the users table cannot hold.

    ``users.language`` is checked to ``am``, ``om`` and ``en``, and ``offer_received`` carries
    all three; there is no seller the platform could store who has no variant. This stands in
    for one anyway, so the fallback is exercised by the code that would meet it if a fourth
    language were ever stored before its translations landed.
    """

    value = "ti"


class _SellerInThatLanguage:
    """The only attribute the offer path reads off an account."""

    language = _LanguageWithNoVariant()


async def _seller_without_a_variant(self: UserRepository, user_id: uuid.UUID) -> Any:
    return _SellerInThatLanguage()


async def test_placing_an_offer_queues_the_sellers_message(
    offer_app: tuple[FastAPI, httpx.AsyncClient],
    settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """A bid writes a QUEUED row for the seller: the template, the lane, the words, the key."""
    _, http = offer_app
    seller = await _sign_in(http, code_for, settings, SELLER, language="en")
    buyer = await _sign_in(http, code_for, settings, BUYER)
    offer_id = await _place_offer(http, seller, buyer, listing_body, offer_body)

    rows = _notifications(postgres_url)
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == _user_id(postgres_url, SELLER)
    assert row["channel"] == "SMS"
    assert row["template_id"] == "offer_received"
    assert row["priority"] == priority_rank(SmsPriority.Q2) == 2
    assert row["language"] == "en"
    assert row["status"] == "QUEUED"
    assert row["dedup_key"] == f"offer_received:{offer_id}"
    assert row["attempt_count"] == 0
    assert row["rendered_text"] == EXPECTED_ENGLISH


async def test_a_notification_that_cannot_be_written_leaves_no_offer_behind(
    offer_app: tuple[FastAPI, httpx.AsyncClient],
    settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The message is in the offer's transaction: break the write and the offer is gone too.

    This seeds its own fresh listing and buyer, so the pair the offer would occupy is unique
    and the counts below are about this placement and nothing a prior test left lying around.
    """
    _, http = offer_app
    seller = await _sign_in(http, code_for, settings, SELLER, language="en")
    buyer = await _sign_in(http, code_for, settings, BUYER)
    posted = await http.post(
        "/v1/listings", headers=seller, json=listing_body(delivery_options=["PICKUP"])
    )
    assert posted.status_code == 201, posted.text
    listing_id = str(posted.json()["id"])

    async def _refuse_to_queue(self: NotificationRepository, notification: Notification) -> Any:
        raise RuntimeError("the notification write is broken on purpose")

    monkeypatch.setattr(NotificationRepository, "add", _refuse_to_queue)

    with pytest.raises(RuntimeError, match="broken on purpose"):
        await http.post(f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body())

    assert _offers(postgres_url) == []
    assert _notifications(postgres_url) == []


async def test_a_placed_offer_reaches_the_seller_through_the_drain(
    offer_app: tuple[FastAPI, httpx.AsyncClient],
    settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """The whole road: a bid queues the words, and the drain puts those exact words in the air."""
    _, http = offer_app
    seller = await _sign_in(http, code_for, settings, SELLER, language="en")
    buyer = await _sign_in(http, code_for, settings, BUYER)
    await _place_offer(http, seller, buyer, listing_body, offer_body)

    report = await _drain(settings, sms_recorder, now=NOON_EAT_UTC)

    assert (report.sent, report.queued, report.failed) == (1, 0, 0)
    row = _notifications(postgres_url)[0]
    assert row["status"] == "SENT"
    assert row["sent_at"] == NOON_EAT_UTC
    assert _delivered_bodies(sms_recorder, SELLER)[-1] == EXPECTED_ENGLISH


async def test_a_q2_notification_is_held_over_quiet_hours_end_to_end(
    offer_app: tuple[FastAPI, httpx.AsyncClient],
    settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """A bid at 22:00 EAT waits for 06:00 rather than ringing a seller asleep.

    The offer is placed normally and only the drain's clock is controlled: the same row a real
    bid writes is held through the window, spends no attempt waiting, and goes out the moment
    the window closes.
    """
    _, http = offer_app
    seller = await _sign_in(http, code_for, settings, SELLER, language="en")
    buyer = await _sign_in(http, code_for, settings, BUYER)
    await _place_offer(http, seller, buyer, listing_body, offer_body)

    held = await _drain(settings, sms_recorder, now=datetime(2026, 10, 16, 22, 0, tzinfo=EAT_ZONE))

    assert (held.queued, held.sent, held.failed) == (1, 0, 0)
    row = _notifications(postgres_url)[0]
    assert row["status"] == "QUEUED"
    assert row["attempt_count"] == 0
    assert row["next_attempt_at"] == datetime(2026, 10, 17, 6, 0, tzinfo=EAT_ZONE).astimezone(UTC)
    assert EXPECTED_ENGLISH not in _delivered_bodies(sms_recorder, SELLER)

    sent = await _drain(settings, sms_recorder, now=datetime(2026, 10, 17, 6, 1, tzinfo=EAT_ZONE))

    assert (sent.sent, sent.queued, sent.failed) == (1, 0, 0)
    row = _notifications(postgres_url)[0]
    assert row["status"] == "SENT"
    assert row["attempt_count"] == 1
    assert _delivered_bodies(sms_recorder, SELLER)[-1] == EXPECTED_ENGLISH


async def test_the_sellers_number_is_masked_in_the_drains_log(
    offer_app: tuple[FastAPI, httpx.AsyncClient],
    settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The send is logged by who and by masked number, never by the number itself."""
    caplog.set_level("INFO", logger="gebeyalink.notifications")
    _, http = offer_app
    seller = await _sign_in(http, code_for, settings, SELLER, language="en")
    buyer = await _sign_in(http, code_for, settings, BUYER)
    await _place_offer(http, seller, buyer, listing_body, offer_body)

    await _drain(settings, sms_recorder, now=NOON_EAT_UTC)

    seen = caplog.text
    assert "notification sent" in seen
    assert str(_user_id(postgres_url, SELLER)) in seen
    assert mask_phone(SELLER) in seen
    assert SELLER not in seen


async def test_a_seller_whose_language_has_no_variant_reads_the_fallback(
    offer_app: tuple[FastAPI, httpx.AsyncClient],
    settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A language the template does not carry still gets a message, in English, and says so.

    ``users.language`` cannot hold a language the registry has no variant for, so the seller's
    language is monkeypatched: the bid still lands, and the row records the language of the
    words it actually holds — the fallback's English, not the language that was asked for.
    """
    _, http = offer_app
    seller = await _sign_in(http, code_for, settings, SELLER, language="en")
    buyer = await _sign_in(http, code_for, settings, BUYER)
    monkeypatch.setattr(UserRepository, "find_by_id", _seller_without_a_variant)

    offer_id = await _place_offer(http, seller, buyer, listing_body, offer_body)

    row = _notifications(postgres_url)[0]
    assert row["dedup_key"] == f"offer_received:{offer_id}"
    assert row["language"] == "en"
    assert row["rendered_text"] == EXPECTED_ENGLISH


async def test_an_offer_fails_when_the_seller_cannot_be_read(
    offer_app: tuple[FastAPI, httpx.AsyncClient],
    settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seller the listing's key guarantees is missing fails the bid rather than being skipped.

    A message is owed with the offer, so an offer whose message cannot be addressed is not an
    offer the platform may stand. The whole placement fails and nothing is written.
    """
    _, http = offer_app
    seller = await _sign_in(http, code_for, settings, SELLER, language="en")
    buyer = await _sign_in(http, code_for, settings, BUYER)
    posted = await http.post(
        "/v1/listings", headers=seller, json=listing_body(delivery_options=["PICKUP"])
    )
    assert posted.status_code == 201, posted.text
    listing_id = str(posted.json()["id"])

    async def _no_such_seller(self: UserRepository, user_id: uuid.UUID) -> Any:
        return None

    monkeypatch.setattr(UserRepository, "find_by_id", _no_such_seller)

    refused = await http.post(f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body())

    assert refused.status_code == 500, refused.text
    assert refused.json()["error"]["code"] == "INTERNAL_ERROR"
    assert _offers(postgres_url) == []
    assert _notifications(postgres_url) == []


async def test_an_offer_fails_when_the_crop_cannot_be_read(
    offer_app: tuple[FastAPI, httpx.AsyncClient],
    settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crop the listing's foreign key guarantees is missing fails the bid the same way.

    The message names the crop, so a read that cannot name it cannot write the message, and the
    offer goes with it rather than being written without a sentence the seller could read.
    """
    _, http = offer_app
    seller = await _sign_in(http, code_for, settings, SELLER, language="en")
    buyer = await _sign_in(http, code_for, settings, BUYER)
    posted = await http.post(
        "/v1/listings", headers=seller, json=listing_body(delivery_options=["PICKUP"])
    )
    assert posted.status_code == 201, posted.text
    listing_id = str(posted.json()["id"])

    async def _no_such_crop(self: CropRepository, crop_id: int) -> Any:
        return None

    monkeypatch.setattr(CropRepository, "find_by_id", _no_such_crop)

    refused = await http.post(f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body())

    assert refused.status_code == 500, refused.text
    assert refused.json()["error"]["code"] == "INTERNAL_ERROR"
    assert _offers(postgres_url) == []
    assert _notifications(postgres_url) == []
