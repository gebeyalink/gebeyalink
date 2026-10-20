"""AC-3.1 — the confirmation a USSD publish owes, and the two doors it is not owed through.

The acceptance criterion is about a USSD session: the farmer finishes the create-listing menu
(§14.3), a listing exists, and the farmer receives a confirmation SMS carrying its short code.
This file is that sentence in code, plus the two doors it deliberately does not cover — a
listing posted by text is already answered inline by its own ``post_success`` reply, and no
acceptance criterion asks to text a seller who posted from the app — so that a later change
cannot quietly start texting either.

The row is read out of PostgreSQL rather than from the service that wrote it, and the message
is read back out of the messaging backend after the drain, because "queued" and "sent" are two
different promises and AC-3.1 is about the second one.

The application's lifespan runs inside each test's own event loop, for the reason the drain's
own tests give: the engine is a process-wide singleton bound to the loop that built it, so the
request that posts the listing and the drain that sends the message must share one loop.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
import psycopg
import psycopg.rows
import pytest
from fastapi import FastAPI

from apps.api import worker
from apps.api.core.config import Settings
from apps.api.db.session import get_session_factory
from apps.api.dependencies import get_listing_service
from apps.api.main import create_app
from apps.api.models.listing import ListingSource
from apps.api.providers.sms import RecordingMessagingProvider, RecordingSmsProvider
from packages.shared.messaging.queues import SmsPriority, priority_rank
from packages.shared.messaging.signature import sign_gateway_body

pytestmark = pytest.mark.integration

SELLER = "+251911000081"

#: Noon UTC, which is 15:00 EAT: comfortably outside the quiet window, for a test whose point
#: is the send rather than the wait.
NOON_EAT_UTC = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

#: The wizard's five screens: my listings, new listing, the first crop, quantity, price,
#: harvest, pickup-only delivery.
WIZARD_KEYS = ["1", "1", "1", "500", "2500", "1", "1"]

_PUBLISHED = re.compile(r"^END Listing (?P<code>\w+) published\.")


@pytest.fixture()
async def confirmation_app(
    settings: Settings,
    clean_database: None,
    clean_store: None,
    sms_recorder: RecordingSmsProvider,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    """The application and an HTTP client on this test's own event loop."""
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        application.state.sms_provider = sms_recorder
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://confirmation.test"
        ) as http:
            yield application, http


def _sync_url(postgres_url: str) -> str:
    return postgres_url.replace("postgresql+psycopg://", "postgresql://")


def _notifications(postgres_url: str) -> list[dict[str, Any]]:
    """Every message owed, read straight out of PostgreSQL.

    A fresh connection every time, so the answer is what has been committed rather than what
    some still-open transaction is holding.
    """
    with psycopg.connect(_sync_url(postgres_url), row_factory=psycopg.rows.dict_row) as connection:
        return list(
            connection.execute(
                "SELECT id, user_id, channel, template_id, priority, language, rendered_text,"
                " status, dedup_key, attempt_count FROM notifications ORDER BY id"
            ).fetchall()
        )


def _user_id(postgres_url: str, phone_e164: str) -> uuid.UUID:
    with psycopg.connect(_sync_url(postgres_url)) as connection:
        row = connection.execute(
            "SELECT id FROM users WHERE phone_e164 = %s", (phone_e164,)
        ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def _listing_id(postgres_url: str) -> uuid.UUID:
    with psycopg.connect(_sync_url(postgres_url)) as connection:
        row = connection.execute("SELECT id FROM listings").fetchone()
    assert row is not None, "the wizard posted no listing"
    return uuid.UUID(str(row[0]))


async def _sign_in(
    http: httpx.AsyncClient,
    code_for: Callable[[str], str],
    settings: Settings,
    *,
    language: str = "en",
) -> dict[str, str]:
    """This seller, signed in over this test's asynchronous client, and given a region."""
    requested = await http.post("/v1/auth/otp/request", json={"phone_e164": SELLER})
    assert requested.status_code == 200, requested.text
    verified = await http.post(
        "/v1/auth/otp/verify",
        json={
            "phone_e164": SELLER,
            "code": code_for(SELLER),
            "terms_version": settings.terms_version,
            "language": language,
        },
    )
    assert verified.status_code == 200, verified.text
    headers = {"Authorization": f"Bearer {verified.json()['access_token']}"}
    updated = await http.patch(
        "/v1/me",
        headers=headers,
        json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
    )
    assert updated.status_code == 200, updated.text
    return headers


async def _dial(
    http: httpx.AsyncClient,
    settings: Settings,
    text: str,
    *,
    session_id: str,
) -> httpx.Response:
    """One keypress to the USSD ingress, signed over the exact bytes posted."""
    body = urlencode({"sessionId": session_id, "phoneNumber": SELLER, "text": text}).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-AT-Signature": sign_gateway_body(settings.gateway_secret.get_secret_value(), body),
    }
    return await http.post("/v1/ussd/session", content=body, headers=headers)


async def _publish_by_ussd(http: httpx.AsyncClient, settings: Settings, *, session_id: str) -> str:
    """The five screens and their keys, walked to the end; the short code they publish."""
    await _dial(http, settings, "", session_id=session_id)
    reply: httpx.Response | None = None
    for key in WIZARD_KEYS:
        reply = await _dial(http, settings, key, session_id=session_id)
    assert reply is not None
    match = _PUBLISHED.match(reply.text)
    assert match is not None, reply.text
    return match.group("code")


async def _drain(settings: Settings, sms_recorder: RecordingSmsProvider) -> Any:
    return await worker.run_notification_drain(
        settings, now=NOON_EAT_UTC, provider=RecordingMessagingProvider(recorder=sms_recorder)
    )


def _texted_with(sms_recorder: RecordingSmsProvider, code: str) -> list[str]:
    """The bodies this seller received that carry that listing's short code.

    Counted against the code rather than against everything the recorder holds, because the
    recorder also holds the OTP the seller signed in with and that message names no listing.
    """
    return [message.body for message in sms_recorder.messages_to(SELLER) if code in message.body]


async def test_a_ussd_publish_owes_the_seller_one_confirmation_sms(
    confirmation_app: tuple[FastAPI, httpx.AsyncClient],
    settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """AC-3.1: the wizard's listing, and the confirmation carrying its short code.

    The replay at the end is the gateway retry the publish screen has to survive: the final
    key may be delivered twice, and a seller is texted once either way.
    """
    _, http = confirmation_app
    await _sign_in(http, code_for, settings)

    code = await _publish_by_ussd(http, settings, session_id="ses-ac31-publish")

    rows = _notifications(postgres_url)
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == _user_id(postgres_url, SELLER)
    assert row["channel"] == "SMS"
    assert row["template_id"] == "listing_created"
    assert row["priority"] == priority_rank(SmsPriority.Q2) == 2
    assert row["language"] == "en"
    assert row["status"] == "QUEUED"
    assert row["attempt_count"] == 0
    assert row["dedup_key"] == f"listing_created:{_listing_id(postgres_url)}"
    assert row["rendered_text"] == f"Your listing is live. Buyers can find it now. Code {code}"

    replayed = await _dial(http, settings, WIZARD_KEYS[-1], session_id="ses-ac31-publish")
    assert replayed.text.startswith("END Listing "), replayed.text
    assert len(_notifications(postgres_url)) == 1

    report = await _drain(settings, sms_recorder)
    assert report.sent == 1
    assert _texted_with(sms_recorder, code) == [
        f"Your listing is live. Buyers can find it now. Code {code}"
    ]


async def test_a_listing_posted_by_text_is_not_texted_a_second_time(
    confirmation_app: tuple[FastAPI, httpx.AsyncClient],
    settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """The SMS command already answers its own POST with `post_success`: one message, not two.

    Written through the same service the inbound gateway calls, with the door that door
    names, rather than through the HTTP endpoint the app uses.
    """
    _, http = confirmation_app
    headers = await _sign_in(http, code_for, settings)
    assert headers

    async with get_session_factory()() as session:
        service = get_listing_service(session)
        response = await service.create(
            _user_id(postgres_url, SELLER),
            dict(listing_body(delivery_options=["PICKUP"])),
            key="ses-ac31-sms:publish_listing:new",
            source=ListingSource.SMS,
        )
    assert response.short_code

    assert _notifications(postgres_url) == []
    report = await _drain(settings, sms_recorder)
    assert report.sent == 0
    assert _texted_with(sms_recorder, response.short_code) == []


async def test_a_listing_posted_by_the_app_is_not_texted(
    confirmation_app: tuple[FastAPI, httpx.AsyncClient],
    settings: Settings,
    code_for: Callable[[str], str],
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """No acceptance criterion asks to text an app seller, so no acceptance criterion is met
    by texting one."""
    _, http = confirmation_app
    headers = await _sign_in(http, code_for, settings)

    posted = await http.post(
        "/v1/listings", headers=headers, json=listing_body(delivery_options=["PICKUP"])
    )
    assert posted.status_code == 201, posted.text

    assert _notifications(postgres_url) == []
    report = await _drain(settings, sms_recorder)
    assert report.sent == 0
    assert _texted_with(sms_recorder, str(posted.json()["short_code"])) == []
