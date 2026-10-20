"""the agent role and the farmer relationships an agent carries

Two tables, because the two facts they hold are different facts.

``user_roles`` is the extra label an account may carry beyond its one primary activity
(§11.1: ``user_id``, ``role`` enum(``AGENT``, ``COLLECTION_AGENT``), PRIMARY KEY
(``user_id``, ``role``)). ``users.role`` stays the single primary label a person is known
by — ``FARMER`` or ``BUYER`` — and this junction is what lets the same person also be an
agent. Only ``AGENT`` is produced by this slice; ``COLLECTION_AGENT`` is carried in the
enum because the vocabulary is one list and the check is what keeps it one list.

``agent_assignments`` is the explicit, dated, revocable relationship FR-1707 asks for: the
agent's ``user_id`` is the farmer, the ``agent_id`` is the agent, and the row exists only
while it is neither revoked nor expired. A partial unique index over
(``user_id``, ``agent_id``) covers the active rows alone, so one farmer has at most one
live assignment to one agent while the history of revoked ones is kept.

Three choices are worth stating rather than leaving to be inferred.

    ``expires_at`` is ``NOT NULL``. FR-1707's relationship is a twelve-month thing that
    must be renewed by the farmer's own re-confirmation, so an assignment without an end
    would be the thing the requirement forbids. The writer sets it to creation plus twelve
    months; the column only holds it.

    The partial unique index rather than a unique constraint, because the uniqueness is a
    property of the active rows. A revoked assignment is history and a farmer may be taken
    on again later, which means the pair (farmer, agent) legitimately appears more than
    once across time and only once at a time.

    ``revoked_at`` is nullable and its nullness is the revocation marker. Revoking is an
    ``UPDATE`` of this column, never a delete: FR-1203's audit trail and a support question
    both need the relationship that ended, and a deleted row answers neither.

Both foreign keys reference ``users.id`` with no cascade, as every account reference in the
platform does: accounts are never hard-deleted, so there is nothing for a cascade to do.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The additional roles an account may carry. Only AGENT has a producer today; the other
#: member is the vocabulary §11.1 names, held here so the check and the model agree.
USER_ROLE = postgresql.ENUM(
    "AGENT",
    "COLLECTION_AGENT",
    name="user_role",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    # Created explicitly, before the table that uses it, and dropped only by the downgrade
    # that created it: a type created here is this migration's to remove.
    USER_ROLE.create(bind, checkfirst=True)

    op.create_table(
        "user_roles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", USER_ROLE, nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_roles_user_id_users"),
        ),
        # §11.1 names (user_id, role) as the primary key: an account either carries a role
        # or it does not, and there is no third state for a surrogate key to describe.
        sa.PrimaryKeyConstraint("user_id", "role", name=op.f("pk_user_roles")),
    )

    op.create_table(
        "agent_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # The farmer the agent acts for.
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # The agent who acts.
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Twelve months from creation, not null: FR-1707's relationship is a dated one that
        # the farmer renews by re-confirming.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # Null while the relationship stands. Null is meaningful rather than missing: it is
        # the revocation marker, and revoking is an update of this column and never a delete.
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_agent_assignments_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["users.id"],
            name=op.f("fk_agent_assignments_agent_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_assignments")),
    )
    # Uniqueness over the *active* rows alone. A revoked assignment is history and a farmer
    # may be taken on again later, so the pair may appear many times across time and at most
    # once at a time — which is exactly what a partial index says and a plain unique
    # constraint cannot.
    op.create_index(
        "uq_agent_assignments_active",
        "agent_assignments",
        ["user_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    # The two read paths this table exists for: an agent listing their farmers, and a
    # farmer's assignments being looked up. Both filter on the active rows first.
    op.create_index(
        "ix_agent_assignments_agent_id",
        "agent_assignments",
        ["agent_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_assignments_user_id",
        "agent_assignments",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_assignments_user_id", table_name="agent_assignments")
    op.drop_index("ix_agent_assignments_agent_id", table_name="agent_assignments")
    op.drop_index("uq_agent_assignments_active", table_name="agent_assignments")
    op.drop_table("agent_assignments")
    op.drop_table("user_roles")
    bind = op.get_bind()
    USER_ROLE.drop(bind, checkfirst=True)
