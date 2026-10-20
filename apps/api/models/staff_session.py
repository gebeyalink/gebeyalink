"""A staff sign-in that the server can end.

A signed token cannot be un-issued. When a staff member's role changes, their password
changes, or their account is disabled, the sessions already open under the old state must stop
working — and the only way to end a stateless token is to keep a row that says it is over. This
is that row.

``expires_at`` is the authority on whether a session is live. The token carries its own ``exp``
and that is still checked, but the dependency reads this column rather than trusting the token
alone, so a verifier whose clock runs fast cannot hold a session open past the moment this row
says it ended. The row is written at login from ``admin_jwt_ttl_seconds``.

``revoked_at`` is the off switch and is an update, never a delete. A revoked session is what a
"this token was used after it was ended" question is answered from, so the row stays.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base


class StaffSession(Base):
    """One staff sign-in, and its window."""

    __tablename__ = "staff_sessions"
    __table_args__ = (
        # The dependency's read ("this staff member's live sessions") and the revocation's
        # write both hit this pair.
        Index("ix_staff_sessions_staff_user_id_revoked_at", "staff_user_id", "revoked_at"),
        Index("ix_staff_sessions_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    staff_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("staff_users.id", name="fk_staff_sessions_staff_user_id_staff_users"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # The authority on whether this session is live. Written from admin_jwt_ttl_seconds.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(postgresql.INET(), nullable=True)
