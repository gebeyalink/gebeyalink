"""Draining the queue of messages owed to traders.

A message is written into ``notifications`` by whatever event owes it and then waits. This
module is the half that sends: a task on a cadence takes a bounded batch of due rows, highest
lane first, and for each one takes the row alone, decides what should happen to it, and writes
that decision down. It is the same shape as the deadline sweeps beside it, because it is the
same problem — a clock acting on rows nobody is asking about — and it keeps the same two
promises: one transaction per row, and a failure on one row does not abandon the rest.

Three decisions are worth stating rather than leaving to be inferred.

    A held message does not spend an attempt. The quiet window says "not now", not "this
    failed": the row's ``next_attempt_at`` moves to the moment the window closes and its
    ``attempt_count`` is untouched, so a message that waited three nights has still had no
    attempt made on it.

    A refused send is a failed attempt, and so is a send that raised. The provider's reason is
    logged but not stored: the vocabulary on the row is the platform's four codes, and a
    gateway's own words are not one of them. A refusal that keeps being refused walks the
    ladder and ends at ``MAX_ATTEMPTS``, which is a fact about the platform rather than about
    the gateway.

    Delivery is at least once. ``dedup_key`` makes one row per event; it does not make one
    send per row. A process that dies between the gateway accepting a message and this module
    committing the row will find the row still QUEUED and send it again, and a duplicate
    message is the price of never losing one.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from apps.api.models.notification import (
    Notification,
    NotificationFailureReason,
    NotificationStatus,
)
from apps.api.models.user import AccountStatus
from apps.api.providers.sms import mask_phone
from apps.api.repositories.notification_repository import NotificationRepository
from packages.shared.messaging.provider import MessagingProvider, SendResult
from packages.shared.messaging.queues import (
    EAT_ZONE,
    QUIET_HOURS_END_EAT,
    SmsPriority,
    priority_from_rank,
    should_queue_until_quiet_hours,
)

logger = logging.getLogger("gebeyalink.notifications")

#: How many due messages one run takes. A drain is a sweep, not a table scan: a backlog
#: larger than this settles across several ticks rather than in one long transaction.
NOTIFICATION_DRAIN_BATCH_SIZE: Final[int] = 100

#: The gap before each retry, by the attempt that just failed: five minutes, then fifteen,
#: then sixty. Three gaps beside four attempts - the first send and three retries.
RETRY_BACKOFFS: Final[tuple[timedelta, ...]] = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(minutes=60),
)

#: Attempts a message gets before it is given up on: the first send plus one per backoff.
MAX_ATTEMPTS: Final[int] = len(RETRY_BACKOFFS) + 1


class Outcome(StrEnum):
    """What happened to one row, in the words the run's log line counts by."""

    SENT = "sent"
    QUEUED = "queued"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DrainReport:
    """One drain's tally: messages sent, still queued, and given up on."""

    sent: int
    queued: int
    failed: int


def next_quiet_hours_end(now: datetime) -> datetime:
    """The first 06:00 EAT strictly after ``now``, returned in UTC.

    The quiet window is a wall-clock fact on the Ethiopia calendar, so the candidate is built
    in EAT and converted back: 23:00 EAT waits for tomorrow's 06:00, while 03:00 EAT waits for
    today's. Strictly after, so a message held at exactly the boundary is not held forever.
    """
    eat = now.astimezone(EAT_ZONE)
    candidate = eat.replace(
        hour=QUIET_HOURS_END_EAT.hour,
        minute=QUIET_HOURS_END_EAT.minute,
        second=0,
        microsecond=0,
    )
    if candidate <= eat:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


class NotificationService:
    """The queue's drain: read the due rows, act on each one alone."""

    def __init__(
        self,
        *,
        notifications: NotificationRepository,
        provider: MessagingProvider,
        batch_size: int = NOTIFICATION_DRAIN_BATCH_SIZE,
    ) -> None:
        self._notifications = notifications
        self._provider = provider
        self._batch_size = batch_size

    async def drain(self, now: datetime | None = None) -> DrainReport:
        """Send what is due and report what happened, one message per transaction.

        The list is read first, unlocked; each id is then taken on its own and either sent,
        held, failed or left alone. A message another drain has already taken, or that moved
        out of ``QUEUED`` while this one waited for its lock, is simply skipped - the queue
        does not need to be told what somebody else did with it.
        """
        moment = now or datetime.now(UTC)
        notification_ids = await self._notifications.list_queued_ids(moment, limit=self._batch_size)
        sent = queued = failed = 0
        for notification_id in notification_ids:
            outcome = await self._drain_one(notification_id, moment)
            if outcome is Outcome.SENT:
                sent += 1
            elif outcome is Outcome.QUEUED:
                queued += 1
            elif outcome is Outcome.FAILED:
                failed += 1
        logger.info("notification drain sent=%s queued=%s failed=%s", sent, queued, failed)
        return DrainReport(sent=sent, queued=queued, failed=failed)

    async def _drain_one(self, notification_id: uuid.UUID, moment: datetime) -> Outcome | None:
        """Take one message through its next step, or leave it exactly as it was."""
        try:
            notification = await self._notifications.find_by_id_for_update_skip_locked(
                notification_id
            )
            if notification is None or notification.status is not NotificationStatus.QUEUED:
                # Another drain is sending it, or it is no longer queued. Nothing to write.
                await self._notifications.rollback()
                return None
            if notification.next_attempt_at is not None and notification.next_attempt_at > moment:
                # Not due yet: a retry or a quiet-window hold that has not come round.
                await self._notifications.rollback()
                return None

            target = await self._notifications.find_user_delivery_target(notification.user_id)
            if target is None or target[1] is not AccountStatus.ACTIVE:
                # A closed or suspended account cannot receive, and will not in five minutes
                # either. Fail once rather than retry a recipient who is not there.
                notification.status = NotificationStatus.FAILED
                notification.failure_reason = NotificationFailureReason.RECIPIENT_UNAVAILABLE.value
                notification.next_attempt_at = None
                await self._notifications.commit()
                logger.info(
                    "notification failed reason=RECIPIENT_UNAVAILABLE user_id=%s",
                    notification.user_id,
                )
                return Outcome.FAILED

            phone_e164, _status = target
            lane = priority_from_rank(notification.priority)
            if should_queue_until_quiet_hours(moment, lane):
                # Held, not attempted: the clock moves, the attempt count does not.
                notification.next_attempt_at = next_quiet_hours_end(moment)
                await self._notifications.commit()
                logger.info(
                    "notification held until=%s lane=%s user_id=%s to=%s",
                    notification.next_attempt_at.isoformat(),
                    lane,
                    notification.user_id,
                    mask_phone(phone_e164),
                )
                return Outcome.QUEUED

            result = await self._send(notification, phone_e164, lane)
            if result is not None and result.accepted:
                notification.status = NotificationStatus.SENT
                notification.sent_at = moment
                notification.gateway_ref = result.provider_ref
                notification.attempt_count += 1
                await self._notifications.commit()
                logger.info(
                    "notification sent lane=%s user_id=%s to=%s",
                    lane,
                    notification.user_id,
                    mask_phone(phone_e164),
                )
                return Outcome.SENT

            return await self._record_failed_attempt(notification, phone_e164, moment)
        except Exception:
            # One message's failure is not the batch's: roll back whatever this row left
            # half-written, log it, and let the loop take the next candidate.
            logger.exception("notification drain failed notification_id=%s", notification_id)
            await self._notifications.rollback()
            return None

    async def _send(
        self, notification: Notification, phone_e164: str, lane: SmsPriority
    ) -> SendResult | None:
        """Ask the provider to send, turning a raise into a refused attempt.

        The row is locked while this awaits: two drains must not both be sending the same
        message, and the lock is what serialises them. A provider that raises is a failed
        attempt like a provider that refused; the exception is logged and the ladder walks.
        """
        try:
            return await self._provider.send(
                phone_e164,
                notification.rendered_text,
                priority=lane,
                template_id=notification.template_id,
            )
        except Exception:
            logger.exception(
                "notification send raised user_id=%s to=%s",
                notification.user_id,
                mask_phone(phone_e164),
            )
            return None

    async def _record_failed_attempt(
        self, notification: Notification, phone_e164: str, moment: datetime
    ) -> Outcome:
        """Spend an attempt: schedule the next rung, or give the message up."""
        notification.attempt_count += 1
        if notification.attempt_count >= MAX_ATTEMPTS:
            notification.status = NotificationStatus.FAILED
            notification.failure_reason = NotificationFailureReason.MAX_ATTEMPTS.value
            # A row that has been given up on has no next attempt, so the moment the last
            # failure was scheduled for is cleared rather than left to read as still pending.
            notification.next_attempt_at = None
            await self._notifications.commit()
            logger.info(
                "notification failed reason=MAX_ATTEMPTS attempts=%s user_id=%s to=%s",
                notification.attempt_count,
                notification.user_id,
                mask_phone(phone_e164),
            )
            return Outcome.FAILED
        notification.next_attempt_at = moment + RETRY_BACKOFFS[notification.attempt_count - 1]
        await self._notifications.commit()
        logger.info(
            "notification retry attempt=%s next=%s user_id=%s to=%s",
            notification.attempt_count,
            notification.next_attempt_at.isoformat(),
            notification.user_id,
            mask_phone(phone_e164),
        )
        return Outcome.QUEUED
