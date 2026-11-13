"""the dated fee policy and the review queue that changes it

Two tables, one rule. ``fee_config`` holds the platform's fee policy as dated rows so that a
change closes one row and opens the next instead of overwriting history. ``pending_changes``
is the generic maker/checker queue: a staff member submits a typed payload, a different staff
member approves it, and the approval is the transaction that applies it.

The migration seeds one policy row with FR-602's published numbers: buyer-funded, two percent
(``200`` basis points), a five-birr floor (``500`` santim), a five-hundred-birr ceiling
(``50000`` santim), and fifteen percent VAT (``1500`` basis points). The values live in the
database now because the money arithmetic reads this row rather than module constants. A
missing active row is a deployment error, not an invitation to fall back.

The partial unique index on ``fee_config`` is what makes "the current fee" a single fact. It
allows any number of closed rows and exactly one row whose ``effective_to`` is null.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEE_PAYER = postgresql.ENUM(
    "BUYER",
    "SELLER",
    "SPLIT_50",
    name="fee_payer",
    create_type=False,
)

PENDING_CHANGE_STATUS = postgresql.ENUM(
    "PENDING",
    "APPROVED",
    "REJECTED",
    name="pending_change_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    PENDING_CHANGE_STATUS.create(bind, checkfirst=True)

    op.create_table(
        "fee_config",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("fee_payer", FEE_PAYER, nullable=False),
        # Basis points: 200 is two percent, 1500 is fifteen percent.
        sa.Column("platform_fee_bps", sa.Integer(), nullable=False),
        sa.Column("min_fee_santim", sa.BigInteger(), nullable=False),
        sa.Column("max_fee_santim", sa.BigInteger(), nullable=False),
        sa.Column("vat_bps", sa.Integer(), nullable=False),
        sa.Column(
            "effective_from",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        # No foreign key. A fee row is financial history and must survive staff-table
        # maintenance; see the model's docstring.
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("change_reason", sa.String(length=500), nullable=True),
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
        sa.CheckConstraint(
            "platform_fee_bps > 0", name=op.f("ck_fee_config_platform_fee_bps_positive")
        ),
        sa.CheckConstraint(
            "vat_bps >= 0 AND vat_bps <= 10000",
            name=op.f("ck_fee_config_vat_bps_range"),
        ),
        sa.CheckConstraint(
            "min_fee_santim >= 0",
            name=op.f("ck_fee_config_min_fee_santim_non_negative"),
        ),
        sa.CheckConstraint(
            "max_fee_santim >= min_fee_santim",
            name=op.f("ck_fee_config_max_fee_santim_not_below_minimum"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name=op.f("ck_fee_config_effective_window_ordered"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fee_config")),
    )
    op.create_index(
        "uq_fee_config_one_active",
        "fee_config",
        ["effective_to"],
        unique=True,
        postgresql_where=sa.text("effective_to IS NULL"),
    )

    op.create_table(
        "pending_changes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # A plain string: the kinds of change that need review will grow, and that is a
        # service-level vocabulary rather than a reason to alter a database type.
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column(
            "status",
            PENDING_CHANGE_STATUS,
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("submitted_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.String(length=500), nullable=True),
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
        sa.CheckConstraint(
            "status <> 'PENDING' OR (approved_by IS NULL AND rejected_by IS NULL)",
            name=op.f("ck_pending_changes_pending_has_no_reviewer"),
        ),
        sa.CheckConstraint(
            "status <> 'APPROVED' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL "
            "AND rejected_by IS NULL AND rejected_at IS NULL)",
            name=op.f("ck_pending_changes_approved_shape"),
        ),
        sa.CheckConstraint(
            "status <> 'REJECTED' OR (rejected_by IS NOT NULL AND rejected_at IS NOT NULL "
            "AND rejection_reason IS NOT NULL AND approved_by IS NULL AND approved_at IS NULL)",
            name=op.f("ck_pending_changes_rejected_shape"),
        ),
        sa.CheckConstraint(
            "approved_by IS NULL OR approved_by <> submitted_by",
            name=op.f("ck_pending_changes_approval_by_different_account"),
        ),
        sa.CheckConstraint(
            "rejected_by IS NULL OR rejected_by <> submitted_by",
            name=op.f("ck_pending_changes_rejection_by_different_account"),
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by"],
            ["staff_users.id"],
            name=op.f("fk_pending_changes_submitted_by_staff_users"),
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["staff_users.id"],
            name=op.f("fk_pending_changes_approved_by_staff_users"),
        ),
        sa.ForeignKeyConstraint(
            ["rejected_by"],
            ["staff_users.id"],
            name=op.f("fk_pending_changes_rejected_by_staff_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pending_changes")),
    )
    op.create_index(
        "ix_pending_changes_status_kind",
        "pending_changes",
        ["status", "kind"],
        unique=False,
    )

    # FR-602's published policy. This is the only row the migration writes; every later change
    # goes through maker/checker and closes this one rather than updating it.
    op.execute(sa.text("""
            INSERT INTO fee_config (
                fee_payer,
                platform_fee_bps,
                min_fee_santim,
                max_fee_santim,
                vat_bps,
                effective_from
            ) VALUES (
                'BUYER',
                200,
                500,
                50000,
                1500,
                now()
            )
            """))


def downgrade() -> None:
    op.drop_index("ix_pending_changes_status_kind", table_name="pending_changes")
    op.drop_table("pending_changes")
    op.drop_index("uq_fee_config_one_active", table_name="fee_config")
    op.drop_table("fee_config")
    PENDING_CHANGE_STATUS.drop(op.get_bind(), checkfirst=True)
