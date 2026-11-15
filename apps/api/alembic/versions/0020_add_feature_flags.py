"""feature flags as data, with the two money gates seeded off

The two switches that used to be process configuration — cash-on-delivery and escrow — become
rows in ``feature_flags``. A row may also carry an optional percentage rollout and an optional
list of region codes, so a pilot can be widened without another deploy; the two gates seeded
here carry neither, which means "when enabled, on for everyone".

Only the two flags that already existed are seeded. ``mpesa_enabled`` is deliberately not
invented by this migration: no reader in the application names it yet, and a flag with no
reader is a row that can drift out of step with a future slice's decision. The payment-mode
wiring and its tests are the reason these two exist now.

Both seeded rows are disabled. That is the platform's existing default, and it makes a fresh
deployment start from the same place the environment variables did: the modes exist as code,
but checkout will refuse them until a checker has approved turning them on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feature_flags",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        # Null means no percentage rollout. Zero is a valid value and means nobody.
        sa.Column("percentage_rollout", sa.Integer(), nullable=True),
        # Null means no region restriction. An empty array is refused below so a flag cannot
        # be configured as enabled in no region at all.
        sa.Column("region_targeting", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
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
            "percentage_rollout IS NULL "
            "OR (percentage_rollout >= 0 AND percentage_rollout <= 100)",
            name=op.f("ck_feature_flags_percentage_rollout_range"),
        ),
        sa.CheckConstraint(
            "region_targeting IS NULL OR cardinality(region_targeting) >= 1",
            name=op.f("ck_feature_flags_region_targeting_not_empty"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feature_flags")),
        sa.UniqueConstraint("name", name=op.f("uq_feature_flags_name")),
    )

    # The two rows the payment-mode readers name. They are off, with neither scope set, which
    # is exactly the shape the old environment variables described.
    op.execute(sa.text("""
            INSERT INTO feature_flags (
                name, enabled, percentage_rollout, region_targeting, description
            )
            VALUES
                (
                    'cod_enabled',
                    false,
                    NULL,
                    NULL,
                    'Whether checkout may create cash-on-delivery orders.'
                ),
                (
                    'escrow_enabled',
                    false,
                    NULL,
                    NULL,
                    'Whether buyers may fund an order the platform holds until delivery.'
                )
            """))


def downgrade() -> None:
    op.drop_table("feature_flags")
