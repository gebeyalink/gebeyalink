"""The extra roles an account may carry beyond its one primary activity.

``users.role`` is the single label a person is primarily known by — a farmer or a buyer —
and it decides which journeys a handset is offered. It is not the whole of what a person may
be. An agent is a farmer or a buyer *as well as* an agent, and §11.1 holds that second fact
in this junction rather than in the primary label, so the two questions — "what is this
account primarily" and "what else may it do" — stay separate.

``AGENT`` is the one this slice grants. ``COLLECTION_AGENT`` is §11.1's other member and is
carried so the enum is one list; nothing here writes it.

A granted role is a row, and a revoked role is the same row with ``revoked_at`` set. The
row is never deleted: whether an account was an agent, and when it stopped, is a question a
support ticket and an audit trail both ask.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base
from apps.api.db.enums import stored_values


class Role(enum.StrEnum):
    """The additional roles §11.1 names.

    Only ``AGENT`` has a producer today. ``COLLECTION_AGENT`` is the other member of the
    vocabulary and sits here so the database check and the model cannot disagree.
    """

    AGENT = "AGENT"
    COLLECTION_AGENT = "COLLECTION_AGENT"


class UserRole(Base):
    """One additional role held by one account, and when it was granted or taken away."""

    __tablename__ = "user_roles"

    # §11.1's primary key is (user_id, role), so there is no surrogate id and no timestamp
    # mixin: the row's own identity is the pair, and its dates are the two below.
    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_user_roles_user_id_users"),
        primary_key=True,
    )
    role: Mapped[Role] = mapped_column(
        SqlEnum(Role, name="user_role", native_enum=True, values_callable=stored_values),
        primary_key=True,
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Null while the role stands. Revoking is an update of this column, never a delete.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
