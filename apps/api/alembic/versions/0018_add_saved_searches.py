"""the saved_searches table

``saved_searches`` is the §11.1 row behind FR-406: the §9.4 filter set, remembered under an
account, with the two digest choices beside it. The ten-per-user limit is enforced by the
service under a row lock; the table itself carries the columns and the checks that keep a
hand-written statement from writing a state the application has no meaning for.

``last_digest_at`` is additive under ADR-0004's silence, not a deviation from it. The ADR
names the §9.4 filter set, the two digest choices and the eleventh-save refusal, but it does
not lock the table's bookkeeping columns; this one is internal, absent from the response
contract, and is what "new since the last digest" is measured from after the first one.

``digest_channel`` reuses the existing ``notification_channel`` type. The API admits only
PUSH and SMS, so a check constraint keeps the database's third member (INAPP) out of a
column whose published contract cannot express it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_searches",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("crop_id", sa.SmallInteger(), nullable=True),
        sa.Column("region_code", sa.String(length=2), nullable=True),
        sa.Column("woreda_id", sa.Integer(), nullable=True),
        sa.Column("radius_km", sa.SmallInteger(), nullable=True),
        sa.Column("price_min", sa.BigInteger(), nullable=True),
        sa.Column("price_max", sa.BigInteger(), nullable=True),
        sa.Column("harvest_date_from", sa.Date(), nullable=True),
        sa.Column("harvest_date_to", sa.Date(), nullable=True),
        sa.Column("organic", sa.Boolean(), nullable=True),
        sa.Column(
            "delivery_option",
            postgresql.ENUM(
                "PICKUP",
                "SELLER_DELIVERY",
                "TRANSPORT_PARTNER",
                name="delivery_option",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("seller_tier_min", sa.String(length=6), nullable=True),
        sa.Column("digest", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "digest_channel",
            postgresql.ENUM(
                "SMS",
                "PUSH",
                "INAPP",
                name="notification_channel",
                create_type=False,
            ),
            server_default="SMS",
            nullable=False,
        ),
        sa.Column("last_digest_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("fk_saved_searches_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["crop_id"],
            ["crops.id"],
            name=op.f("fk_saved_searches_crop_id_crops"),
        ),
        sa.ForeignKeyConstraint(
            ["region_code"],
            ["regions.code"],
            name=op.f("fk_saved_searches_region_code_regions"),
        ),
        sa.ForeignKeyConstraint(
            ["woreda_id"],
            ["woredas.id"],
            name=op.f("fk_saved_searches_woreda_id_woredas"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_searches")),
    )
    op.create_index(op.f("ix_saved_searches_user_id"), "saved_searches", ["user_id"], unique=False)

    op.create_check_constraint(
        op.f("ck_saved_searches_region_code_length"),
        "saved_searches",
        sa.text("region_code IS NULL OR char_length(region_code) = 2"),
    )
    op.create_check_constraint(
        op.f("ck_saved_searches_radius_km_range"),
        "saved_searches",
        sa.text("radius_km IS NULL OR radius_km BETWEEN 1 AND 500"),
    )
    op.create_check_constraint(
        op.f("ck_saved_searches_seller_tier_min"),
        "saved_searches",
        sa.text("seller_tier_min IS NULL OR seller_tier_min IN ('TIER_0', 'TIER_1', 'TIER_2')"),
    )
    op.create_check_constraint(
        op.f("ck_saved_searches_digest_channel"),
        "saved_searches",
        sa.text("digest_channel IN ('PUSH', 'SMS')"),
    )


def downgrade() -> None:
    op.drop_table("saved_searches")
