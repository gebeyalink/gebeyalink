"""Add the accounts table

Revision ID: 0001
Revises:
Create Date: 2026-10-03

The first table in the platform: the person. Everything a trader does hangs off this row,
and it is created the moment they prove they can receive a message on their number.

No credentials are stored here. Sign-in is by verification code, so there is no password
column to leak, to reset, or to phish.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # The identifier a trader has before they have an account, and the anchor that
        # makes signing in again land on the same account instead of a duplicate.
        sa.Column("phone_e164", sa.String(length=13), nullable=False),
        sa.Column("role", sa.String(length=8), nullable=False),
        sa.Column("language", sa.String(length=2), server_default="am", nullable=False),
        sa.Column("kyc_tier", sa.String(length=6), server_default="TIER_0", nullable=False),
        sa.Column("status", sa.String(length=9), server_default="ACTIVE", nullable=False),
        # Proof the trader accepted the platform's terms. Trading is only allowed once
        # this is filled in, which is why both columns live on the account.
        sa.Column("terms_version", sa.String(length=20), nullable=True),
        sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("phone_e164", name=op.f("uq_users_phone_e164")),
    )
    op.create_index(op.f("ix_users_phone_e164"), "users", ["phone_e164"], unique=True)

    # Role, language, verification tier, and status are each a closed set that the
    # application already knows. A check constraint keeps a typo in a hand-written
    # statement from putting a value nothing will ever read back.
    op.create_check_constraint(
        op.f("ck_users_role"), "users", sa.text("role IN ('FARMER', 'BUYER')")
    )
    op.create_check_constraint(
        op.f("ck_users_language"), "users", sa.text("language IN ('am', 'om', 'en')")
    )
    op.create_check_constraint(
        op.f("ck_users_kyc_tier"), "users", sa.text("kyc_tier IN ('TIER_0', 'TIER_1', 'TIER_2')")
    )
    op.create_check_constraint(
        op.f("ck_users_status"),
        "users",
        sa.text("status IN ('ACTIVE', 'RECOVERY', 'COOLDOWN', 'FROZEN', 'SUSPENDED', 'CLOSED')"),
    )
    # The format check lives in the database as well as in the application, because
    # anything that can write to this table can bypass the application.
    op.create_check_constraint(
        op.f("ck_users_phone_e164"),
        "users",
        sa.text("phone_e164 ~ '^\\+251(9|7)[0-9]{8}$'"),
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_users_phone_e164"), "users", type_="check")
    op.drop_constraint(op.f("ck_users_status"), "users", type_="check")
    op.drop_constraint(op.f("ck_users_kyc_tier"), "users", type_="check")
    op.drop_constraint(op.f("ck_users_language"), "users", type_="check")
    op.drop_constraint(op.f("ck_users_role"), "users", type_="check")
    op.drop_index(op.f("ix_users_phone_e164"), table_name="users")
    op.drop_table("users")
