"""Reading and writing queued notifications.

The repository is the only place that speaks SQL for messages. The drain above it asks two
questions in two deliberately different shapes. Which rows are due is a plain read that locks
nothing and is bounded: the answer is a list of identities, and holding locks while merely
asking which messages the platform owes would make two drains wait on each other over rows
neither has begun to send. Taking one row to send is a read that locks exactly that row and
skips any row another drain already holds, so two drains never send the same message twice and
neither waits for the other.

The delivery target is read here too, because it is a read of the same database in the same
transaction: a message goes to the number the account holds and only while the account is
active, and both are on the user rather than on the message.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.notification import Notification, NotificationStatus
from apps.api.models.user import AccountStatus, User


class NotificationRepository:
    """Queued-message reads, and the writes that queue one and commit one."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, notification: Notification) -> Notification:
        """Queue one message, and deliberately do not commit it.

        ``session.add`` and ``flush`` and nothing else, the same contract as
        ``OfferRepository.add``: the row is written into the caller's transaction and the
        caller decides when it lands. This is what lets a producer write the message the event
        owes in the same transaction that produced the event — the offer and its notification
        are one commit, so a failure on either leaves neither behind. A caller that invokes
        ``add`` and never commits has written nothing, which is the point.
        """
        self._session.add(notification)
        await self._session.flush()
        return notification

    async def list_queued_ids(self, now: datetime, *, limit: int) -> list[uuid.UUID]:
        """The next batch of due messages, highest lane first.

        The order is the specification's: lane ascending (Q1 stored as 1 before Q2 and Q3),
        then the earliest moment each row becomes due, then the row's identity. ``NULLS
        FIRST`` is what keeps a row that has never been attempted ahead of one that is
        waiting on a retry at the same lane, and the identity is the tie-breaker that makes
        the order total rather than merely stable-looking.

        Deliberately unlocked, and bounded. This asks which rows are due; the lock belongs to
        the send, not to the question.
        """
        stmt = (
            select(Notification.id)
            .where(
                Notification.status == NotificationStatus.QUEUED,
                (Notification.next_attempt_at.is_(None)) | (Notification.next_attempt_at <= now),
            )
            .order_by(
                Notification.priority.asc(),
                Notification.next_attempt_at.asc().nulls_first(),
                Notification.id.asc(),
            )
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def find_by_id_for_update_skip_locked(
        self, notification_id: uuid.UUID
    ) -> Notification | None:
        """Take this row for the length of its send, or answer ``None`` if another has it.

        ``SKIP LOCKED`` rather than a plain ``FOR UPDATE``: a second drain that arrives while
        the first is sending should move on to the next due row rather than wait for a message
        it is not going to send.
        """
        stmt = (
            select(Notification)
            .where(Notification.id == notification_id)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_user_delivery_target(
        self, user_id: uuid.UUID
    ) -> tuple[str, AccountStatus] | None:
        """The number a message would go to and the account's state, if the account is there.

        ``None`` means no such account. The foreign key makes that unreachable for a row the
        drain selected, but a lookup that cannot find one answers nothing rather than guessing
        a number to send to.
        """
        result = await self._session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            return None
        return user.phone_e164, user.status

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        """Discard an unfinished change and release the transaction's row lock.

        The drain's per-row transaction is either written or abandoned, and the lock it took
        has to go back the moment it decides not to write, or the next due row waits behind a
        message nothing is going to happen to.
        """
        await self._session.rollback()
