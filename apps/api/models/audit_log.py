"""The record of what was done, and by whom.

§11.1 names this table as the append-only trail: every staff action, and every agent action
taken on a farmer's behalf, written once and never changed. Its value is entirely in that
"never changed" — a log that can be rewritten answers nothing a question about history needs —
so the database enforces it. A ``BEFORE UPDATE OR DELETE`` trigger raises for every writer
including the schema owner, which is the strongest form available to a single-role deployment.

Why a trigger rather than a refused grant: the application and the migrations connect as the
database's own superuser, so ``REVOKE UPDATE, DELETE`` would bind nobody who actually writes.
The trigger binds everyone, and removing it takes a migration — a deliberate act with a trail
of its own.

``actor_id`` carries no foreign key. An audit row may name a staff account *or* a trader account
(an agent onboarding a farmer acts as themselves), and a column that points at one of two tables
cannot point at either with a constraint. ``actor_role`` says which kind of actor it was.

``before_hash`` and ``after_hash`` are SHA-256 hex of the pre/post state, not the state itself.
The log records that a change happened and what shape it had, without becoming a second copy of
the data it describes — a copy that would drift, and that would have to be as carefully guarded
as the tables it duplicates.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CHAR, DateTime, Index, String, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base


class AuditLog(Base):
    """One action, its actor, its target, and the state on either side of it."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_actor_id_at", "actor_id", "at"),
        Index("ix_audit_log_target_type_target_id", "target_type", "target_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # No foreign key: may name a staff account or a trader account. Null for a system action.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(postgresql.UUID(as_uuid=True), nullable=True)
    # A plain string, not an enum: 'ADMIN', 'VERIFIER', 'AGENT', 'SYSTEM'. Widening an audit
    # vocabulary should not need a type migration.
    actor_role: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )
    before_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    after_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    ip: Mapped[str | None] = mapped_column(postgresql.INET(), nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
