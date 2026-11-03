"""FR-1003: at most ten non-transactional SMS per user per EAT day.

The cap is counted when a message is sent, not when it is queued, and Q1 - an authentication
or pickup message - is exempt. These tests drive the real drain against PostgreSQL and read
the rows back, so the count is taken from the table rather than from the drain's own report.
A message held by the cap is still a message the trader is owed; here it is recorded as
FAILED with ``CAP_EXCEEDED`` and the moment the EAT day turns.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from apps.api.core.config import Settings
from apps.api.providers.sms import RecordingSmsProvider
from apps.api.tests.test_notification_drain import (  # noqa: F401 - drain_app is a fixture
    _drain,
    _insert_notification,
    _insert_user,
    _row,
    drain_app,
)
from packages.shared.messaging.queues import EAT_ZONE, SmsPriority, priority_rank

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("drain_app")]

USER_A_PHONE = "+251911000031"
USER_B_PHONE = "+251911000032"

#: Noon on the Ethiopia calendar, comfortably outside the quiet window.
DAY_ONE_NOON_EAT = datetime(2026, 10, 16, 12, 0, tzinfo=EAT_ZONE)
DAY_TWO_NOON_EAT = DAY_ONE_NOON_EAT + timedelta(days=1)

#: The first 00:00 EAT after day one's noon, expressed in UTC as the row stores it.
NEXT_EAT_MIDNIGHT = datetime(2026, 10, 17, 0, 0, tzinfo=EAT_ZONE).astimezone(UTC)


async def _send_ten_non_transactional(
    postgres_url: str,
    user_id: uuid.UUID,
    settings: Settings,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """Put ten Q3 rows in the queue and drain them at day one's noon."""
    for _ in range(10):
        _insert_notification(postgres_url, user_id, priority=priority_rank(SmsPriority.Q3))
    report = await _drain(settings, sms_recorder, now=DAY_ONE_NOON_EAT)
    assert (report.sent, report.queued, report.failed) == (10, 0, 0)


async def test_ten_non_transactional_messages_all_send(
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """A day's first ten non-transactional messages each reach the provider."""
    user_id = _insert_user(postgres_url, USER_A_PHONE)
    notification_ids = [
        _insert_notification(postgres_url, user_id, priority=priority_rank(SmsPriority.Q3))
        for _ in range(10)
    ]

    report = await _drain(settings, sms_recorder, now=DAY_ONE_NOON_EAT)

    assert (report.sent, report.queued, report.failed) == (10, 0, 0)
    for notification_id in notification_ids:
        row = _row(postgres_url, notification_id)
        assert row["status"] == "SENT"
        assert row["sent_at"] == DAY_ONE_NOON_EAT
    assert len(sms_recorder.messages_to(USER_A_PHONE)) == 10


async def test_the_eleventh_is_failed_with_cap_exceeded_and_not_sent(
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """The eleventh text is refused before the provider is called."""
    user_id = _insert_user(postgres_url, USER_A_PHONE)
    await _send_ten_non_transactional(postgres_url, user_id, settings, sms_recorder)
    eleventh = _insert_notification(postgres_url, user_id, priority=priority_rank(SmsPriority.Q3))

    report = await _drain(settings, sms_recorder, now=DAY_ONE_NOON_EAT)

    assert (report.sent, report.queued, report.failed) == (0, 0, 1)
    row = _row(postgres_url, eleventh)
    assert row["status"] == "FAILED"
    assert row["failure_reason"] == "CAP_EXCEEDED"
    assert row["next_attempt_at"] == NEXT_EAT_MIDNIGHT
    assert len(sms_recorder.messages_to(USER_A_PHONE)) == 10


async def test_a_q1_message_is_exempt_from_the_cap(
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """An authentication or pickup message sends even after ten non-transactional ones."""
    user_id = _insert_user(postgres_url, USER_A_PHONE)
    await _send_ten_non_transactional(postgres_url, user_id, settings, sms_recorder)
    urgent = _insert_notification(postgres_url, user_id, priority=priority_rank(SmsPriority.Q1))

    report = await _drain(settings, sms_recorder, now=DAY_ONE_NOON_EAT)

    assert (report.sent, report.queued, report.failed) == (1, 0, 0)
    assert _row(postgres_url, urgent)["status"] == "SENT"
    assert len(sms_recorder.messages_to(USER_A_PHONE)) == 11


async def test_the_next_eat_day_moves_the_window(
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """A message sent yesterday no longer counts once the EAT calendar day has turned."""
    user_id = _insert_user(postgres_url, USER_A_PHONE)
    await _send_ten_non_transactional(postgres_url, user_id, settings, sms_recorder)
    twelfth = _insert_notification(postgres_url, user_id, priority=priority_rank(SmsPriority.Q3))

    report = await _drain(settings, sms_recorder, now=DAY_TWO_NOON_EAT)

    assert (report.sent, report.queued, report.failed) == (1, 0, 0)
    assert _row(postgres_url, twelfth)["status"] == "SENT"


async def test_the_cap_is_per_user_not_per_system(
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """One user's ten texts do not spend another user's allowance."""
    user_a = _insert_user(postgres_url, USER_A_PHONE)
    await _send_ten_non_transactional(postgres_url, user_a, settings, sms_recorder)
    user_b = _insert_user(postgres_url, USER_B_PHONE)
    first_for_b = _insert_notification(postgres_url, user_b, priority=priority_rank(SmsPriority.Q3))

    report = await _drain(settings, sms_recorder, now=DAY_ONE_NOON_EAT)

    assert (report.sent, report.queued, report.failed) == (1, 0, 0)
    assert _row(postgres_url, first_for_b)["status"] == "SENT"
    assert len(sms_recorder.messages_to(USER_B_PHONE)) == 1


async def test_only_sent_messages_count_toward_the_cap(
    settings: Settings,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    """Five failed rows and five sent rows are five texts spent, so the sixth still sends."""
    user_id = _insert_user(postgres_url, USER_A_PHONE)
    for _ in range(5):
        _insert_notification(postgres_url, user_id, priority=priority_rank(SmsPriority.Q3))
    first = await _drain(settings, sms_recorder, now=DAY_ONE_NOON_EAT)
    assert first.sent == 5

    for _ in range(5):
        _insert_notification(
            postgres_url,
            user_id,
            priority=priority_rank(SmsPriority.Q3),
            status="FAILED",
            failure_reason="MAX_ATTEMPTS",
        )
    sixth = _insert_notification(postgres_url, user_id, priority=priority_rank(SmsPriority.Q3))

    report = await _drain(settings, sms_recorder, now=DAY_ONE_NOON_EAT)

    assert (report.sent, report.queued, report.failed) == (1, 0, 0)
    assert _row(postgres_url, sixth)["status"] == "SENT"
    assert len(sms_recorder.messages_to(USER_A_PHONE)) == 6
