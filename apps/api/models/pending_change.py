"""A proposed change waiting for a second pair of eyes.

Maker/checker is one rule applied to several kinds of change: one staff member proposes, a
different staff member approves, and the change takes effect only in that second transaction.
The table is therefore generic rather than a fee table with a reviewer column. ``kind`` says
which applier understands ``payload``; the row itself does not know what a fee is.

``kind`` is a plain string on purpose. The set of changes that require review will grow as the
platform's money surfaces grow, and widening a vocabulary of change kinds should be a service
decision rather than a database type migration. The applier registry is the authority on which
kinds are understood.

The three statuses are a closed lifecycle: a row is born ``PENDING`` and reaches exactly one of
``APPROVED`` or ``REJECTED``. The check constraint carries that shape into the database so a
hand-written statement cannot leave a row in a state the service would not have produced.

Self-review is refused twice. The service compares the actor with ``submitted_by`` before it
writes anything, and the check constraints below refuse a row whose reviewer is its maker. The
service check is the one that produces a useful HTTP error; the database check means there is
no second code path that can bypass it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base, TimestampMixin
from apps.api.db.enums import stored_values


class PendingChangeStatus(StrEnum):
    """Where a proposed change is in its review lifecycle."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class PendingChange(TimestampMixin, Base):
    """One proposed change and the two staff accounts around it."""

    __tablename__ = "pending_changes"
    __table_args__ = (
        CheckConstraint(
            "status <> 'PENDING' OR (approved_by IS NULL AND rejected_by IS NULL)",
            name="pending_has_no_reviewer",
        ),
        CheckConstraint(
            "status <> 'APPROVED' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL "
            "AND rejected_by IS NULL AND rejected_at IS NULL)",
            name="approved_shape",
        ),
        CheckConstraint(
            "status <> 'REJECTED' OR (rejected_by IS NOT NULL AND rejected_at IS NOT NULL "
            "AND rejection_reason IS NOT NULL AND approved_by IS NULL AND approved_at IS NULL)",
            name="rejected_shape",
        ),
        CheckConstraint(
            "approved_by IS NULL OR approved_by <> submitted_by",
            name="approval_by_different_account",
        ),
        CheckConstraint(
            "rejected_by IS NULL OR rejected_by <> submitted_by",
            name="rejection_by_different_account",
        ),
        Index("ix_pending_changes_status_kind", "status", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[PendingChangeStatus] = mapped_column(
        SqlEnum(
            PendingChangeStatus,
            name="pending_change_status",
            values_callable=stored_values,
        ),
        server_default=PendingChangeStatus.PENDING.value,
        nullable=False,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB(astext_type=Text()), nullable=False
    )

    submitted_by: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("staff_users.id"),
        nullable=False,
    )

    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("staff_users.id"),
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    rejected_by: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("staff_users.id"),
        nullable=True,
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # ``created_at`` from TimestampMixin is the submission time. It is not duplicated under a
    # second name: two columns carrying one instant are two columns that can disagree.
