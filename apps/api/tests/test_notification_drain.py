"""Draining the queue: what a message does between being written and being sent.

This slice is the sending half alone. Nothing writes a notification yet, so every row these
tests drain is one they wrote themselves; what is proven here is the drain's own decisions —
the lane order, the quiet window that holds rather than fails, the retry ladder and the fourth
attempt that gives up, the recipient whose account is gone, the row that is no longer queued,
and the lock that keeps two drains from sending one message twice. Each row is read back out of
PostgreSQL rather than out of the run's own report, because the row is the record and the tally
is only the run's account of it.

The application's lifespan runs inside each test's own event loop. The engine is a process-wide
singleton bound to the loop that built it, so a test that drove the synchronous client and then
awaited the drain would have the drain querying a pool owned by another loop; starting the
lifespan here is what keeps the two on one loop.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import psycopg.rows
import pytest
from fastapi import FastAPI
from psycopg import errors as psycopg_errors

from apps.api import worker
from apps.api.core.config import Settings
from apps.api.db.session import get_session_factory
from apps.api.main import create_app
from apps.api.providers.sms import (
    RecordingMessagingProvider,
    RecordingSmsProvider,
    mask_phone,
)
from apps.api.repositories.notification_repository import NotificationRepository
from apps.api.services.notification_service import DrainReport, NotificationService
from packages.shared.messaging.provider import MessagingProvider, SendResult
from packages.shared.messaging.queues import EAT_ZONE, SmsPriority, priority_rank

pytestmark = pytest.mark.integration

ACTIVE_PHONE = "+251911000021"
SUSPENDED_PHONE = "+251911000022"
OTHER_PHONE = "+251911000023"

#: The words a row carries. A stored body and not a template: the drain sends exactly what the
#: row says, which is what makes a queued message a message rather than a promise of one.
RENDERED = "That action code did not work: n/a"

#: The two registry ids these tests name. ``action_token_invalid`` is Q1 and carries no
#: one-time code, which is what lets a Q1 row be seeded here without putting a secret in a table.
Q1_TEMPLATE = "action_token_invalid"
Q3_TEMPLATE = "price_digest_daily"

#: Noon EAT, comfortably outside the quiet window, for tests that are not about the window.
NOON_EAT_UTC = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


@pytest.fixture()
async def drain_app(
    settings: Settings,
    clean_database: None,
    clean_store: None,
    sms_recorder: RecordingSmsProvider,
) -> AsyncIterator[FastAPI]:
    """The application running inside this test's event loop, so the drain shares its engine.

    Only the lifespan is wanted: it is what creates the process's engine and its Redis client,
    and it is what binds them to the loop that will await the drain. The HTTP client the other
    integration fixtures build is left out, because nothing here makes a request.
    """
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        application.state.sms_provider = sms_recorder
        yield application


def _sync_url(postgres_url: str) -> str:
    return postgres_url.replace("postgresql+psycopg://", "postgresql://")


def _insert_user(postgres_url: str, phone: str, *, status: str = "ACTIVE") -> uuid.UUID:
    """Write one account directly, so a test only ever exercises the drain."""
    with psycopg.connect(_sync_url(postgres_url)) as connection:
        row = connection.execute(
            "INSERT INTO users (id, phone_e164, role, status) VALUES (%s, %s, %s, %s)"
            " RETURNING id",
            (uuid.uuid4(), phone, "FARMER", status),
        ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def _insert_notification(
    postgres_url: str,
    user_id: uuid.UUID,
    *,
    priority: int = priority_rank(SmsPriority.Q2),
    template_id: str | None = None,
    rendered_text: str = RENDERED,
    status: str = "QUEUED",
    failure_reason: str | None = None,
    attempt_count: int = 0,
    next_attempt_at: datetime | None = None,
    dedup_key: str | None = None,
) -> uuid.UUID:
    """Write one queued message straight into the table the drain reads."""
    # The registry id follows the lane unless the caller names one, so a seeded row is the row
    # a real producer of that lane would have written rather than a Q1 row under a Q3 template.
    if template_id is None:
        template_id = Q1_TEMPLATE if priority == priority_rank(SmsPriority.Q1) else Q3_TEMPLATE
    notification_id = uuid.uuid4()
    with psycopg.connect(_sync_url(postgres_url)) as connection:
        connection.execute(
            "INSERT INTO notifications"
            " (id, user_id, channel, template_id, priority, language, rendered_text, status,"
            "  failure_reason, attempt_count, next_attempt_at, dedup_key)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                notification_id,
                user_id,
                "SMS",
                template_id,
                priority,
                "en",
                rendered_text,
                status,
                failure_reason,
                attempt_count,
                next_attempt_at,
                dedup_key,
            ),
        )
    return notification_id


def _row(postgres_url: str, notification_id: uuid.UUID) -> dict[str, Any]:
    with psycopg.connect(_sync_url(postgres_url), row_factory=psycopg.rows.dict_row) as connection:
        row = connection.execute(
            "SELECT status, failure_reason, attempt_count, next_attempt_at, sent_at,"
            " gateway_ref, rendered_text FROM notifications WHERE id = %s",
            (notification_id,),
        ).fetchone()
    assert row is not None, "the notification disappeared"
    return dict(row)


def _count_queued(postgres_url: str) -> int:
    with psycopg.connect(_sync_url(postgres_url)) as connection:
        row = connection.execute(
            "SELECT count(*) FROM notifications WHERE status = 'QUEUED'"
        ).fetchone()
    assert row is not None
    return int(row[0])


def _recording(sms_recorder: RecordingSmsProvider) -> RecordingMessagingProvider:
    return RecordingMessagingProvider(recorder=sms_recorder)


async def _drain(
    settings: Settings,
    sms_recorder: RecordingSmsProvider,
    *,
    now: datetime,
    provider: MessagingProvider | None = None,
) -> DrainReport:
    return await worker.run_notification_drain(
        settings, now=now, provider=provider or _recording(sms_recorder)
    )


class _RefusingProvider:
    """A gateway that answers every send with a refusal, in its own words."""

    async def send(
        self, phone_e164: str, body: str, *, priority: SmsPriority, template_id: str
    ) -> SendResult:
        return SendResult(accepted=False, reason="insufficient credit")


class _RaisingProvider:
    """A gateway that breaks instead of refusing: the exception a drain has to survive."""

    async def send(
        self, phone_e164: str, body: str, *, priority: SmsPriority, template_id: str
    ) -> SendResult:
        raise RuntimeError("the provider is broken on purpose")


class _GatedProvider:
    """A provider that parks mid-send, so the row stays locked while another drain looks."""

    def __init__(self, inner: RecordingMessagingProvider) -> None:
        self._inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send(
        self, phone_e164: str, body: str, *, priority: SmsPriority, template_id: str
    ) -> SendResult:
        self.entered.set()
        await self.release.wait()
        return await self._inner.send(phone_e164, body, priority=priority, template_id=template_id)


async def test_a_due_message_is_sent_and_recorded(
    drain_app: FastAPI,
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """A queued row in an open lane goes out, and the row records that it did."""
    user_id = _insert_user(postgres_url, ACTIVE_PHONE)
    notification_id = _insert_notification(postgres_url, user_id)

    report = await _drain(settings, sms_recorder, now=NOON_EAT_UTC)

    assert (report.sent, report.queued, report.failed) == (1, 0, 0)
    row = _row(postgres_url, notification_id)
    assert row["status"] == "SENT"
    assert row["sent_at"] == NOON_EAT_UTC
    assert row["attempt_count"] == 1
    assert row["gateway_ref"] is not None
    assert [message.body for message in sms_recorder.messages_to(ACTIVE_PHONE)] == [RENDERED]


async def test_the_drain_reaches_the_highest_lane_before_an_older_lower_one(
    drain_app: FastAPI,
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """A hundred older Q3 rows do not crowd out a newer Q1 row: the lane decides, not the age.

    The batch is exactly full, so if the queue were ordered by age the Q1 row would be the one
    left behind. It goes first instead, and one Q3 row is the row that waits.
    """
    user_id = _insert_user(postgres_url, ACTIVE_PHONE)
    for _ in range(100):
        _insert_notification(postgres_url, user_id, priority=priority_rank(SmsPriority.Q3))
    urgent = _insert_notification(postgres_url, user_id, priority=priority_rank(SmsPriority.Q1))

    report = await _drain(settings, sms_recorder, now=NOON_EAT_UTC)

    assert report.sent == 100
    assert _row(postgres_url, urgent)["status"] == "SENT"
    assert _count_queued(postgres_url) == 1


async def test_one_drain_sends_a_bounded_batch(
    drain_app: FastAPI,
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """A backlog larger than one batch settles across runs, not in one long transaction."""
    user_id = _insert_user(postgres_url, ACTIVE_PHONE)
    for _ in range(105):
        _insert_notification(postgres_url, user_id, priority=priority_rank(SmsPriority.Q3))

    report = await _drain(settings, sms_recorder, now=NOON_EAT_UTC)

    assert report.sent == 100
    assert _count_queued(postgres_url) == 5


async def test_a_refused_send_walks_the_ladder_and_gives_up_on_the_fourth_attempt(
    drain_app: FastAPI,
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """Five minutes, then fifteen, then sixty; four attempts and then the row is given up on."""
    user_id = _insert_user(postgres_url, ACTIVE_PHONE)
    notification_id = _insert_notification(
        postgres_url, user_id, priority=priority_rank(SmsPriority.Q2)
    )
    provider = _RefusingProvider()
    start = NOON_EAT_UTC

    first = await _drain(settings, sms_recorder, now=start, provider=provider)
    assert (first.queued, first.sent, first.failed) == (1, 0, 0)
    row = _row(postgres_url, notification_id)
    assert (row["attempt_count"], row["status"]) == (1, "QUEUED")
    assert row["next_attempt_at"] == start + timedelta(minutes=5)

    await _drain(
        settings, sms_recorder, now=start + timedelta(minutes=5), provider=_RefusingProvider()
    )
    row = _row(postgres_url, notification_id)
    assert (row["attempt_count"], row["next_attempt_at"]) == (2, start + timedelta(minutes=20))

    await _drain(
        settings, sms_recorder, now=start + timedelta(minutes=20), provider=_RefusingProvider()
    )
    row = _row(postgres_url, notification_id)
    assert (row["attempt_count"], row["next_attempt_at"]) == (3, start + timedelta(minutes=80))

    final = await _drain(
        settings, sms_recorder, now=start + timedelta(minutes=80), provider=_RefusingProvider()
    )

    assert (final.failed, final.queued, final.sent) == (1, 0, 0)
    row = _row(postgres_url, notification_id)
    assert row["attempt_count"] == 4
    assert row["status"] == "FAILED"
    assert row["failure_reason"] == "MAX_ATTEMPTS"
    assert row["next_attempt_at"] is None
    assert sms_recorder.sent == []


async def test_a_provider_that_raises_is_a_failed_attempt_not_a_lost_message(
    drain_app: FastAPI,
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception is the same fact as a refusal: the row walks the ladder, masked in the log."""
    caplog.set_level("INFO", logger="gebeyalink.notifications")
    user_id = _insert_user(postgres_url, ACTIVE_PHONE)
    notification_id = _insert_notification(
        postgres_url, user_id, priority=priority_rank(SmsPriority.Q2)
    )

    report = await _drain(settings, sms_recorder, now=NOON_EAT_UTC, provider=_RaisingProvider())

    assert (report.queued, report.failed, report.sent) == (1, 0, 0)
    row = _row(postgres_url, notification_id)
    assert row["attempt_count"] == 1
    assert row["status"] == "QUEUED"
    assert row["next_attempt_at"] == NOON_EAT_UTC + timedelta(minutes=5)
    assert "notification send raised" in caplog.text
    assert mask_phone(ACTIVE_PHONE) in caplog.text
    assert ACTIVE_PHONE not in caplog.text


async def test_a_recipient_whose_account_is_not_active_is_failed_without_a_retry(
    drain_app: FastAPI,
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """A closed account will not be open in five minutes either, so the row stops now."""
    user_id = _insert_user(postgres_url, SUSPENDED_PHONE, status="SUSPENDED")
    notification_id = _insert_notification(
        postgres_url, user_id, priority=priority_rank(SmsPriority.Q2)
    )

    report = await _drain(settings, sms_recorder, now=NOON_EAT_UTC)

    assert (report.failed, report.queued, report.sent) == (1, 0, 0)
    row = _row(postgres_url, notification_id)
    assert row["status"] == "FAILED"
    assert row["failure_reason"] == "RECIPIENT_UNAVAILABLE"
    assert row["attempt_count"] == 0
    assert row["next_attempt_at"] is None
    assert sms_recorder.sent == []


async def test_a_message_already_sent_is_not_sent_again(
    drain_app: FastAPI,
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """The second run finds nothing due, and the trader receives one message and not two."""
    user_id = _insert_user(postgres_url, ACTIVE_PHONE)
    _insert_notification(postgres_url, user_id)

    first = await _drain(settings, sms_recorder, now=NOON_EAT_UTC)
    second = await _drain(settings, sms_recorder, now=NOON_EAT_UTC)

    assert (first.sent, second.sent) == (1, 0)
    assert len(sms_recorder.sent) == 1


async def test_a_row_is_left_alone_when_it_is_not_queued_or_not_due_yet(
    drain_app: FastAPI,
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """The two ways a candidate id turns out to need nothing: it moved on, or its time has not come.

    The list of due rows is read unlocked, so between being read and being taken a row can stop
    being the drain's to act on. Taking it answers that in the negative rather than writing.
    """
    user_id = _insert_user(postgres_url, ACTIVE_PHONE)
    already_sent = _insert_notification(postgres_url, user_id, status="SENT", attempt_count=1)
    not_due = _insert_notification(
        postgres_url,
        user_id,
        priority=priority_rank(SmsPriority.Q2),
        next_attempt_at=NOON_EAT_UTC + timedelta(hours=1),
    )

    async with get_session_factory()() as session:
        service = NotificationService(
            notifications=NotificationRepository(session),
            provider=_recording(sms_recorder),
        )
        assert await service._drain_one(already_sent, NOON_EAT_UTC) is None
        assert await service._drain_one(not_due, NOON_EAT_UTC) is None

    assert sms_recorder.sent == []
    assert _row(postgres_url, already_sent)["status"] == "SENT"
    assert _row(postgres_url, not_due)["status"] == "QUEUED"


async def test_a_failure_on_one_message_does_not_abandon_the_batch(
    drain_app: FastAPI,
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One row's unexpected failure is rolled back and logged; the next row keeps its turn."""
    broken_user = _insert_user(postgres_url, ACTIVE_PHONE)
    intact_user = _insert_user(postgres_url, OTHER_PHONE)
    broken = _insert_notification(postgres_url, broken_user, priority=priority_rank(SmsPriority.Q2))
    intact = _insert_notification(postgres_url, intact_user, priority=priority_rank(SmsPriority.Q2))

    original = NotificationRepository.find_user_delivery_target

    async def _flaky(self: NotificationRepository, user_id: uuid.UUID) -> Any:
        if user_id == broken_user:
            raise RuntimeError("this lookup is broken on purpose")
        return await original(self, user_id)

    monkeypatch.setattr(NotificationRepository, "find_user_delivery_target", _flaky)

    report = await _drain(settings, sms_recorder, now=NOON_EAT_UTC)

    assert report.sent == 1
    assert _row(postgres_url, intact)["status"] == "SENT"
    assert _row(postgres_url, broken)["status"] == "QUEUED"
    assert _row(postgres_url, broken)["attempt_count"] == 0


async def test_two_drains_racing_send_once(
    drain_app: FastAPI,
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """Two drains over one due row send it once between them.

    The barrier holds both at the starting line, so both have listed the row before either has
    taken it. The contention is on the row's own lock, and ``SKIP LOCKED`` is what turns the
    loser away instead of blocking it.
    """
    user_id = _insert_user(postgres_url, ACTIVE_PHONE)
    notification_id = _insert_notification(postgres_url, user_id)
    ready = asyncio.Barrier(2)

    async def drain() -> DrainReport:
        await ready.wait()
        return await _drain(settings, sms_recorder, now=NOON_EAT_UTC)

    first, second = await asyncio.gather(drain(), drain())

    assert first.sent + second.sent == 1
    assert _row(postgres_url, notification_id)["status"] == "SENT"
    assert len(sms_recorder.sent) == 1


async def test_the_row_stays_locked_while_the_provider_is_sending(
    drain_app: FastAPI,
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """The lock is held across the provider call, so a second drain cannot take the row.

    The gated provider parks inside the send, which is the only moment the race is about: the
    first drain holds the row's lock while it awaits the network, and the second drain finds
    nothing it may take rather than sending the same message alongside it.
    """
    user_id = _insert_user(postgres_url, ACTIVE_PHONE)
    notification_id = _insert_notification(postgres_url, user_id)
    gated = _GatedProvider(_recording(sms_recorder))

    first = asyncio.create_task(
        worker.run_notification_drain(settings, now=NOON_EAT_UTC, provider=gated)
    )
    try:
        await asyncio.wait_for(gated.entered.wait(), timeout=10)
        second = await _drain(settings, sms_recorder, now=NOON_EAT_UTC)
        assert second.sent == 0
        assert _row(postgres_url, notification_id)["status"] == "QUEUED"
    finally:
        gated.release.set()

    report = await asyncio.wait_for(first, timeout=10)
    assert report.sent == 1
    assert _row(postgres_url, notification_id)["status"] == "SENT"
    assert len(sms_recorder.sent) == 1


@pytest.mark.parametrize(
    ("eat_hour", "eat_minute", "lane", "sends"),
    [
        (20, 59, SmsPriority.Q2, True),
        (21, 0, SmsPriority.Q2, False),
        (23, 59, SmsPriority.Q2, False),
        (0, 0, SmsPriority.Q2, False),
        (3, 0, SmsPriority.Q2, False),
        (5, 59, SmsPriority.Q2, False),
        (6, 0, SmsPriority.Q2, True),
        # Q1 is the lane a trader must not wait on: it sends at any hour.
        (22, 0, SmsPriority.Q1, True),
    ],
)
async def test_the_quiet_window_holds_a_message_and_hands_back_its_attempt(
    drain_app: FastAPI,
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
    eat_hour: int,
    eat_minute: int,
    lane: SmsPriority,
    sends: bool,
) -> None:
    """The window is a wall clock on the Ethiopian day, and a hold costs no attempt.

    Each case is the minute before, at, and after a boundary. A held row's ``next_attempt_at``
    moves to the close of the window - 06:00 EAT on the same day before dawn, and on the next
    day after the opening edge - and its ``attempt_count`` does not move at all, because waiting
    is not failing.
    """
    user_id = _insert_user(postgres_url, ACTIVE_PHONE)
    notification_id = _insert_notification(postgres_url, user_id, priority=priority_rank(lane))
    now = datetime(2026, 10, 16, eat_hour, eat_minute, tzinfo=EAT_ZONE)

    report = await _drain(settings, sms_recorder, now=now)

    row = _row(postgres_url, notification_id)
    if sends:
        assert report.sent == 1
        assert row["status"] == "SENT"
        assert row["attempt_count"] == 1
        assert len(sms_recorder.sent) == 1
    else:
        expected_close = datetime(
            2026, 10, 16 if eat_hour < 6 else 17, 6, 0, tzinfo=EAT_ZONE
        ).astimezone(UTC)
        assert (report.queued, report.sent, report.failed) == (1, 0, 0)
        assert row["status"] == "QUEUED"
        assert row["attempt_count"] == 0
        assert row["next_attempt_at"] == expected_close
        assert sms_recorder.sent == []


def test_the_failure_vocabulary_is_enforced_by_the_database(
    postgres_url: str, clean_database: None
) -> None:
    """A reason outside the four the constraint admits is refused by the table, not the model."""
    user_id = _insert_user(postgres_url, ACTIVE_PHONE)
    with pytest.raises(psycopg_errors.CheckViolation):
        _insert_notification(postgres_url, user_id, status="FAILED", failure_reason="NOT_A_CODE")


def test_the_dedup_key_admits_one_row_per_event(postgres_url: str, clean_database: None) -> None:
    """A producer that writes the same event twice is refused the second write."""
    user_id = _insert_user(postgres_url, ACTIVE_PHONE)
    _insert_notification(postgres_url, user_id, dedup_key="order-1")
    with pytest.raises(psycopg_errors.UniqueViolation):
        _insert_notification(postgres_url, user_id, dedup_key="order-1")
