"""listings, listing_photos and price_indices

Transcribed from the core-entity definitions in the specification:

    **listings** - `seller_id fk`, `on_behalf_of_user_id fk null`, `source`
    enum(`APP, USSD, SMS, AGENT`), `crop_id fk`, `quantity_kg int`, `sold_kg int
    default 0`, `min_order_kg int`, `unit_price_santim bigint`, `harvest_date date`,
    `available_from timestamptz null`, `expires_at timestamptz`, `region_code char(2)`,
    `woreda_id fk`, `delivery_options delivery_option[] not null`,
    `delivery_fee_santim bigint not null default 0`, `organic bool default false`,
    `description varchar(500)`, `status listing_status not null`, `price_outlier bool
    default false`, `short_code varchar(8) unique` (Crockford Base32),
    `listing_attributes jsonb not null default '{}'`, `version int`.

    **listing_photos** - `listing_id fk`, `url`, `width`, `height`, `sort_order`.

    **price_indices** - `crop_id fk`, `region_code char(2)`, `day date`,
    `avg_unit_price_santim bigint null`, `sample_size int`, `insufficient_data bool`,
    UNIQUE(`crop_id`,`region_code`,`day`).

The preamble covers the rest: `id uuid PK default gen_random_uuid()`, `created_at` and
`updated_at` from the database clock, and `version int not null default 0` because a
listing is a mutable business entity.

Two choices are worth naming, because both are departures from the letter of the list
above and neither changes what a column holds.

`region_code` is varchar rather than char, for the same reason it is on `profiles`: a
char(2) code is blank-padded, so `'OR'` written into it reads back as `'OR '`, which
compares equal but does not read equal. That is a trap for anything that joins on it, and
the two-character length is held by a check constraint instead, as it is there.

`delivery_options` is a real array of a real enum type rather than free text, so that
`'PICKUP'` and `'PICKPU'` cannot both be stored and the set membership the price of a
delivery means can be asked of the database and not only of the application.

`sold_kg` starts at zero and is carried here because an order consumes weight against a
listing; nothing in this slice writes it, and the reservation rules that would are their
own work.

`price_indices` is created empty. It is filled by the overnight aggregation, and the
creation and price-sanity rules read whatever that has published rather than computing an
average inline. An empty table is a truthful answer: no index, no claim about a price.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LISTING_SOURCE = sa.Enum("APP", "USSD", "SMS", "AGENT", name="listing_source")
DELIVERY_OPTION = sa.Enum("PICKUP", "SELLER_DELIVERY", "TRANSPORT_PARTNER", name="delivery_option")
LISTING_STATUS = sa.Enum(
    "DRAFT",
    "ACTIVE",
    "PAUSED",
    "SOLD_OUT",
    "EXPIRED",
    "SUSPENDED",
    "CLOSED",
    name="listing_status",
)


def upgrade() -> None:
    # Every enum here is brought into existence by the creation of the table that holds it,
    # the one inside the array included. Creating any of them separately would ask for the
    # same type twice and be refused the second time.
    op.create_table(
        "listings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # The account that wrote the listing. An agent may write on another's behalf, which
        # is who they wrote it as, so both are recorded and neither is inferred.
        sa.Column("seller_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("on_behalf_of_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", LISTING_SOURCE, nullable=False),
        # The taxonomy's own small number, so a listing and the published crop are the same
        # thing rather than two rows that happen to agree.
        sa.Column("crop_id", sa.SmallInteger(), nullable=False),
        sa.Column("quantity_kg", sa.Integer(), nullable=False),
        sa.Column("sold_kg", sa.Integer(), server_default="0", nullable=False),
        sa.Column("min_order_kg", sa.Integer(), nullable=False),
        sa.Column("unit_price_santim", sa.BigInteger(), nullable=False),
        sa.Column("harvest_date", sa.Date(), nullable=False),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("region_code", sa.String(length=2), nullable=False),
        sa.Column("woreda_id", sa.Integer(), nullable=False),
        sa.Column(
            "delivery_options",
            postgresql.ARRAY(DELIVERY_OPTION),
            nullable=False,
        ),
        sa.Column("delivery_fee_santim", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("organic", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("status", LISTING_STATUS, nullable=False),
        # A price may be flagged and still be posted. This is a warning to the seller and a
        # signal to anyone reading the listing, never a refusal, so it is a plain flag.
        sa.Column("price_outlier", sa.Boolean(), server_default="false", nullable=False),
        # The six-character Crockford Base32 code, with no prefix: orders carry the prefix
        # and a listing does not, so the two are told apart by length rather than by having
        # to strip a marker off one of them. Unique so a code that reaches a trader is
        # theirs alone.
        sa.Column("short_code", sa.String(length=8), nullable=False),
        sa.Column(
            "listing_attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
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
            ["seller_id"],
            ["users.id"],
            name=op.f("fk_listings_seller_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["on_behalf_of_user_id"],
            ["users.id"],
            name=op.f("fk_listings_on_behalf_of_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["crop_id"],
            ["crops.id"],
            name=op.f("fk_listings_crop_id_crops"),
        ),
        sa.ForeignKeyConstraint(
            ["region_code"],
            ["regions.code"],
            name=op.f("fk_listings_region_code_regions"),
        ),
        sa.ForeignKeyConstraint(
            ["woreda_id"],
            ["woredas.id"],
            name=op.f("fk_listings_woreda_id_woredas"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_listings")),
        sa.UniqueConstraint("short_code", name=op.f("uq_listings_short_code")),
    )
    op.create_index(op.f("ix_listings_seller_id"), "listings", ["seller_id"])
    # A seller reads their own listings newest first and a buyer reads live ones filtered by
    # crop and region, so both of those access paths are named here rather than discovered
    # by a sequential scan later.
    op.create_index(
        op.f("ix_listings_seller_id_created_at"),
        "listings",
        ["seller_id", "created_at"],
    )
    op.create_index(
        op.f("ix_listings_crop_id_region_code_status"),
        "listings",
        ["crop_id", "region_code", "status"],
    )
    op.create_index(op.f("ix_listings_status_expires_at"), "listings", ["status", "expires_at"])

    # The weight rules are checks on the shape of what was sent, which is what a foreign key
    # cannot say. They are held here as well as in the service so that a row written by
    # anything else cannot be one the application would have refused.
    op.create_check_constraint(
        op.f("ck_listings_region_code_length"),
        "listings",
        sa.text("char_length(region_code) = 2"),
    )
    op.create_check_constraint(
        op.f("ck_listings_quantity_kg_range"),
        "listings",
        sa.text("quantity_kg BETWEEN 1 AND 1000000"),
    )
    op.create_check_constraint(
        op.f("ck_listings_sold_kg_range"),
        "listings",
        sa.text("sold_kg >= 0 AND sold_kg <= quantity_kg"),
    )
    op.create_check_constraint(
        op.f("ck_listings_min_order_kg_range"),
        "listings",
        sa.text("min_order_kg >= 1 AND min_order_kg <= quantity_kg"),
    )
    op.create_check_constraint(
        op.f("ck_listings_unit_price_santim_positive"),
        "listings",
        sa.text("unit_price_santim > 0"),
    )
    op.create_check_constraint(
        op.f("ck_listings_delivery_fee_santim"),
        "listings",
        sa.text(
            "delivery_fee_santim >= 0 AND "
            "((delivery_fee_santim > 0) = ('SELLER_DELIVERY' = ANY(delivery_options)))"
        ),
    )
    op.create_check_constraint(
        op.f("ck_listings_description_length"),
        "listings",
        sa.text("description IS NULL OR char_length(description) <= 500"),
    )
    op.create_check_constraint(
        op.f("ck_listings_short_code_length"),
        "listings",
        sa.text("char_length(short_code) = 6"),
    )

    op.create_table(
        "listing_photos",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("listing_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.SmallInteger(), nullable=False),
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
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_listing_photos_listing_id_listings"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_listing_photos")),
        # The position is the identity of a photo within a listing: the same position twice
        # would make the order a guess, so the database will not allow it.
        sa.UniqueConstraint(
            "listing_id",
            "sort_order",
            name=op.f("uq_listing_photos_listing_id_sort_order"),
        ),
    )
    op.create_index(op.f("ix_listing_photos_listing_id"), "listing_photos", ["listing_id"])

    op.create_table(
        "price_indices",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("crop_id", sa.SmallInteger(), nullable=False),
        sa.Column("region_code", sa.String(length=2), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        # Null rather than zero when no average could be computed: a zero average is a price,
        # and reading one where there is only an absence would flag every listing wrongly.
        sa.Column("avg_unit_price_santim", sa.BigInteger(), nullable=True),
        sa.Column("sample_size", sa.Integer(), server_default="0", nullable=False),
        # Defaults to true so a row written without a judgement is read as saying nothing.
        sa.Column("insufficient_data", sa.Boolean(), server_default="true", nullable=False),
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
            ["crop_id"],
            ["crops.id"],
            name=op.f("fk_price_indices_crop_id_crops"),
        ),
        sa.ForeignKeyConstraint(
            ["region_code"],
            ["regions.code"],
            name=op.f("fk_price_indices_region_code_regions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_indices")),
        sa.UniqueConstraint(
            "crop_id",
            "region_code",
            "day",
            name=op.f("uq_price_indices_crop_id_region_code_day"),
        ),
    )
    op.create_index(
        op.f("ix_price_indices_crop_id_region_code_day"),
        "price_indices",
        ["crop_id", "region_code", "day"],
    )
    op.create_check_constraint(
        op.f("ck_price_indices_region_code_length"),
        "price_indices",
        sa.text("char_length(region_code) = 2"),
    )
    op.create_check_constraint(
        op.f("ck_price_indices_sample_size"),
        "price_indices",
        sa.text("sample_size >= 0"),
    )


def downgrade() -> None:
    op.drop_table("price_indices")
    op.drop_table("listing_photos")
    op.drop_table("listings")
    LISTING_STATUS.drop(op.get_bind(), checkfirst=True)
    DELIVERY_OPTION.drop(op.get_bind(), checkfirst=True)
    LISTING_SOURCE.drop(op.get_bind(), checkfirst=True)
