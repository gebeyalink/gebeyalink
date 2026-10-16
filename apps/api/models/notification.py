"""A message owed to a trader, waiting its turn to be sent.

A notification is the row between an event and a handset. An event decides a message is owed
and writes it down here, with the text already rendered in the trader's language; a scheduled
task then reads the oldest, highest-lane rows and hands each one to the messaging provider.
Separating the two is what lets a message be sent by a process with no request behind it, and
what lets a message that could not be sent be a row somebody can find rather than a line in a
log.

Four decisions are worth stating rather than leaving to be inferred.

    ``rendered_text`` never holds a phone number and never holds a one-time code. The row is a
    message body in a table, and a body in a table is readable by everyone who can read the
    table; the number a body is addressed to lives on the user, not on the message, and a
    code that unlocks something belongs in the ``verification_codes`` store with the other
    secrets rather than in a notification's copy of the sentence that mentioned it.

    ``priority`` is the lane the template declared, stored as the integer the queue sorts on:
    Q1 is 1, Q2 is 2, Q3 is 3, and lower is earlier. The mapping is not repeated here; it is
    ``priority_rank`` in the messaging package, so the writer and the drain cannot disagree
    about which lane a name is.

    ``dedup_key`` makes one row per event, not one send per row. A producer that writes the
    same event twice is refused by the unique constraint; the drain, by contrast, is
    deliberately at-least-once, because a crash between the gateway accepting a message and
    the row's own commit will ask the gateway again. The deduplication is about what the
    platform owes, and the retry is about what the network did.

    ``failure_reason`` carries a short code from a closed vocabulary - the four in the check
    constraint the migration writes - and null while the row has not failed.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base, TimestampMixin
from apps.api.db.enums import stored_values


class NotificationChannel(enum.StrEnum):
    """Which wire a message goes out on.

    SMS is the one channel the platform can carry today. PUSH and INAPP are the whole
    vocabulary rather than a promise about this deployment: a reader of the column sees the
    same three words regardless of which of them a given slice produced, and an in-app
    notification writes INAPP rather than leaving the field to be inferred from a missing
    value.
    """

    SMS = "SMS"
    PUSH = "PUSH"
    INAPP = "INAPP"


class NotificationStatus(enum.StrEnum):
    """Where a message sits in its own life.

    A message is born QUEUED, becomes SENT once the gateway has taken it, and becomes FAILED
    when the platform has given up on it - the recipient's account is not active, or the
    retry ladder is exhausted. DELIVERED is the vocabulary's fourth member and is reserved:
    the gateway's own delivery receipt is a later slice's, and until then a message that was
    accepted ends at SENT.
    """

    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class NotificationFailureReason(enum.StrEnum):
    """Why a message stopped.

    The four short codes the row's check constraint admits. ``RECIPIENT_UNAVAILABLE`` and
    ``MAX_ATTEMPTS`` are the two the drain produces: the account is not active, or the retry
    ladder ran out. ``CAP_EXCEEDED`` and ``PROVIDER_REJECTED`` are reserved for the slices
    that add the daily cap and surface the gateway's own refusal; the vocabulary is one list
    so a reader never meets a word the constraint has not admitted.
    """

    CAP_EXCEEDED = "CAP_EXCEEDED"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    MAX_ATTEMPTS = "MAX_ATTEMPTS"
    RECIPIENT_UNAVAILABLE = "RECIPIENT_UNAVAILABLE"


class Notification(TimestampMixin, Base):
    """One message, its addressee, its text, and where it has got to."""

    __tablename__ = "notifications"
    __table_args__ = (
        # The four short codes a row's failure reason may hold. Two have producers today and
        # two are reserved, which is why the check names all four: the vocabulary is one list.
        CheckConstraint(
            "failure_reason IS NULL OR failure_reason IN "
            "('CAP_EXCEEDED', 'PROVIDER_REJECTED', 'MAX_ATTEMPTS', 'RECIPIENT_UNAVAILABLE')",
            name="failure_reason",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # A plain foreign key, not a cascade: an account is closed rather than deleted, so there
    # is nothing for a cascade to do and the constraint records that a message has an owner.
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", name="fk_notifications_user_id_users"),
        nullable=False,
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        SqlEnum(NotificationChannel, name="notification_channel", values_callable=stored_values),
        nullable=False,
    )
    # The registry id whose words these are, kept beside the rendered text rather than
    # instead of it: the text is what sends, and the id is what a later reader counts by.
    template_id: Mapped[str] = mapped_column(String(length=64), nullable=False)
    # The template's lane, stored as the integer the drain sorts on. Lower sends sooner.
    priority: Mapped[int] = mapped_column(SmallInteger(), nullable=False)
    language: Mapped[str] = mapped_column(String(length=2), nullable=False)
    rendered_text: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        SqlEnum(NotificationStatus, name="notification_status", values_callable=stored_values),
        nullable=False,
        default=NotificationStatus.QUEUED,
        server_default=NotificationStatus.QUEUED.value,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gateway_ref: Mapped[str | None] = mapped_column(String(length=64), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(length=32), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0"
    )
    # When the row may next be attempted: set by a failed send to the next rung of the retry
    # ladder, and by the quiet window to the moment it closes. Null means "as soon as the
    # queue reaches it".
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # One row per event. A producer that writes the same event twice is refused here.
    dedup_key: Mapped[str | None] = mapped_column(String(length=128), nullable=True, unique=True)
