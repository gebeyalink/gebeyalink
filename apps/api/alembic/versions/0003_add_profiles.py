"""profiles: the trader behind the account

Transcribed from the core-entity definition in the specification:

    profiles — 1:1 with users: `display_name`, `region_code char(2)` (§27.1), `woreda_id
    fk`, `kebele varchar(80)`, `gps geography(Point,4326)`, `avatar_url`,
    `business_name`, `license_no`.

And from the TIN rule: "Commercial tax identity: `profiles.tax_identification_number`
(TIN) captured for business accounts." It is a plain nullable column. The tier that would
consume it is proposed and not active, so nothing here reads it back or branches on it.

The §11 preamble covers the rest: `id uuid PK default gen_random_uuid()`, `created_at` and
`updated_at` from the database clock, and `version int not null default 0` because a profile
is a mutable business entity.

`gps` is written and never read back out. It is here so that a trader's own device can help
them get the location right; it is not published, not logged, and not in the response.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from apps.api.db.geo import GeographyPoint

revision: str = "0003"
down_revision: str = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "profiles",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # One profile per account. The unique constraint is what makes "1:1" true in the
        # database rather than merely intended, so a second row cannot be written at all.
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # How long is 2-60 characters; the database holds the upper bound so a row written
        # by anything other than the API cannot be longer than the rule allows.
        sa.Column("display_name", sa.String(length=60), nullable=True),
        # A two-character region code, held as varchar rather than char so that it is not
        # blank-padded: a padded code compares equal but does not read equal, which is a
        # trap for anything that joins on it.
        sa.Column("region_code", sa.String(length=2), nullable=True),
        sa.Column("woreda_id", sa.Integer(), nullable=True),
        sa.Column("kebele", sa.String(length=80), nullable=True),
        sa.Column("gps", GeographyPoint(), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("business_name", sa.Text(), nullable=True),
        sa.Column("license_no", sa.Text(), nullable=True),
        sa.Column("tax_identification_number", sa.Text(), nullable=True),
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
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_profiles_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["region_code"],
            ["regions.code"],
            name=op.f("fk_profiles_region_code_regions"),
        ),
        sa.ForeignKeyConstraint(
            ["woreda_id"],
            ["woredas.id"],
            name=op.f("fk_profiles_woreda_id_woredas"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profiles")),
        sa.UniqueConstraint("user_id", name=op.f("uq_profiles_user_id")),
    )
    op.create_index(op.f("ix_profiles_user_id"), "profiles", ["user_id"], unique=True)

    # A blank string is not a way to clear a name, and a name that is present at all must
    # satisfy the same two-character rule the account does. Both checks are on the shape of
    # what was sent, which a foreign key cannot express.
    op.create_check_constraint(
        op.f("ck_profiles_region_code_length"),
        "profiles",
        sa.text("region_code IS NULL OR char_length(region_code) = 2"),
    )
    op.create_check_constraint(
        op.f("ck_profiles_display_name_length"),
        "profiles",
        sa.text(
            "display_name IS NULL OR "
            "(char_length(display_name) BETWEEN 2 AND 60 "
            "AND btrim(display_name) = display_name)"
        ),
    )


def downgrade() -> None:
    op.drop_table("profiles")
