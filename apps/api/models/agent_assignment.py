"""The explicit, dated, revocable relationship between an agent and a farmer.

FR-1707 asks for the relationship to be named rather than inferred: an agent acts for a
farmer because there is a row saying so, the row has an end date, and either side can end
it. This model is that row.

``user_id`` is the farmer and ``agent_id`` is the agent — the farmer is the one being
carried, so the plain column name is the person the row is *about*, in the same way a
listing's ``seller_id`` is. The two are never the same account, and the writer refuses that
case rather than leaving it to the database, because it is a rule about agents and not a
shape a table happens to have.

The active relationship is the one whose ``revoked_at`` is null and whose ``expires_at`` is
still ahead; both are read together by anything that asks whether the agent may act. A
revoked assignment is history and stays as a row, which is why revocation updates a column
instead of deleting one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base, TimestampMixin


class AgentAssignment(Base, TimestampMixin):
    """One farmer carried by one agent, until it is revoked or lapses."""

    __tablename__ = "agent_assignments"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The farmer. Named user_id because the row is about the person being carried, matching
    # how a listing's ownership column is named for the owner rather than the visitor.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", name="fk_agent_assignments_user_id_users"),
        nullable=False,
    )
    # The agent. A plain foreign key with no cascade, as every account reference here is:
    # accounts are never hard-deleted.
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", name="fk_agent_assignments_agent_id_users"),
        nullable=False,
    )
    # Twelve months from creation, written by the service. Not null: an assignment without
    # an end is the thing FR-1707 forbids, so the column cannot express one.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Null while the relationship stands; the revocation marker, set and never unset.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
