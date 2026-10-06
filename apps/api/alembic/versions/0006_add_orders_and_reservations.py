"""orders and quantity_reservations

Transcribed from the core-entity definitions in the specification:

    **orders** - `short_code varchar(9) unique`, `buyer_id fk`, `seller_id fk`, `listing_id fk`,
    `offer_id fk null`, `quantity_kg int`, `unit_price_santim bigint`, `delivery_method
    delivery_option`, `delivery_fee_santim bigint`, `platform_fee_santim bigint`, `vat_santim
    bigint`, `total_santim bigint`, `buyer_address_woreda_id fk`, `buyer_address_text
    varchar(300)`, `pickup_code char(6)`, `status` enum(...) not null (§10.2),
    `state_before_dispute` enum(...) null (§10.2), `funded_at`, `shipped_at`, `delivered_at
    timestamptz`, `delivered_weight_kg int null` (FR-2507), `cancel_reason`, `payment_mode`
    enum(`ESCROW, COD`) not null default `ESCROW`, `version int`.

    **quantity_reservations** - `listing_id fk`, `holder_type` enum(`ORDER`), `holder_id`,
    `quantity_kg`, `state` enum(`HELD, CONVERTED, RELEASED, CONSUMED`). (FR-1901)

The preamble covers the rest: `id uuid PK default gen_random_uuid()`, `created_at` and
`updated_at` from the database clock, and `version int not null default 0` because an order is a
mutable business entity.

Six choices are worth naming, because each is a decision the column list leaves open.

`fee_payer` is added to the column list. §11.1 does not name it, and the acceptance rules
require the funding side to be *snapshotted*: an order is settled months later, under a policy
that may since have been changed, and re-reading configuration at settlement would settle it
under terms nobody agreed to at the sale. Without the column the snapshot cannot exist, and a
snapshot that is recomputed on read is not a snapshot.

`state_before_dispute` reuses the same enum type as `status` rather than declaring a second
identical one. It holds an order status or nothing, so a distinct type would be a second
vocabulary for the same eleven words.

`pickup_code` is `varchar(6)` rather than `char(6)`. The specification writes `char(6)`, and
PostgreSQL's `char` pads to fixed width, so a six-digit code read back is six characters either
way — but `char` compares equal to a trimmed `varchar`, which would quietly let `GL-` codes and
pickup codes of different padding match each other in a lookup. `varchar` holds exactly the six
digits that were generated. A check constraint below holds it to six digits regardless.

The two code columns are checked, not merely typed. `short_code` must be `GL-` plus six
characters from Crockford's alphabet, and `pickup_code` must be six digits: both are read aloud
and keyed in by hand, so a row holding something else is a row somebody is going to misread.

`quantity_reservations.holder_id` gets a foreign key to `orders.id`. `holder_type` is an enum
with one member, so the column is redundant with it — that is the point of version 2.1.3's
narrowing, and the foreign key is what makes "only an order can hold kilograms" a constraint
rather than a convention.

The `(listing_id, state)` index is what availability is computed from. Every read of what is
left asks for the sum of the blocking states of one listing, which is that pair exactly, and it
is asked on the accept path where a slow query is a slow sale.

Order matters: reservations reference orders, so orders is created first.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORDER_STATUS = postgresql.ENUM(
    "CREATED",
    "AWAITING_FUNDING",
    "CONFIRMED",
    "FUNDED",
    "READY_FOR_PICKUP",
    "SHIPPED",
    "DELIVERED",
    "COMPLETED",
    "CANCELLED",
    "CANCELLED_REFUNDED",
    "DISPUTED",
    name="order_status",
    create_type=False,
)

PAYMENT_MODE = postgresql.ENUM("ESCROW", "COD", name="payment_mode", create_type=False)

FEE_PAYER = postgresql.ENUM("BUYER", "SELLER", "SPLIT_50", name="fee_payer", create_type=False)

HOLDER_TYPE = postgresql.ENUM("ORDER", name="holder_type", create_type=False)

RESERVATION_STATE = postgresql.ENUM(
    "HELD", "CONVERTED", "RELEASED", "CONSUMED", name="reservation_state", create_type=False
)

#: The delivery-method enum came into existence with the listings table, so it is used rather
#: than created. `create_type=False` on the column is what stops this migration asking for a
#: type that already exists.
DELIVERY_METHOD = postgresql.ENUM(name="delivery_option", create_type=False)

#: Crockford's alphabet leaves out I, L, O and U so a code read aloud cannot be misheard as a
#: different code. Held as a check rather than only as a generator, because the value reaches
#: hands, text messages and keypads, and a row holding a character the alphabet excludes would be
#: a code some reader cannot read.
CROCKFORD_SIX = "0-9A-HJKMNP-TV-Z"

NEW_ENUM_TYPES = (
    ORDER_STATUS,
    PAYMENT_MODE,
    FEE_PAYER,
    HOLDER_TYPE,
    RESERVATION_STATE,
)


def upgrade() -> None:
    bind = op.get_bind()
    # Created here rather than by the table that holds them, because `order_status` is used twice
    # in one table and `delivery_option` is used by a table that already exists. Both cases make
    # the type's creation ambiguous, and an explicit `checkfirst` is the way to be unambiguous
    # rather than lucky.
    for enum_type in NEW_ENUM_TYPES:
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "orders",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # Nine characters: the `GL-` prefix and six of Crockford's alphabet. Stored with the
        # prefix so that the SMS, the app and the USSD menu are all printing the same string.
        sa.Column("short_code", sa.String(length=9), nullable=False),
        sa.Column("buyer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seller_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("listing_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Null when the buyer paid the asking price without bidding, which is a meaningful
        # difference rather than a missing value: it is the difference between an order and a
        # negotiation that ended well.
        sa.Column("offer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("quantity_kg", sa.Integer(), nullable=False),
        sa.Column("unit_price_santim", sa.BigInteger(), nullable=False),
        sa.Column("delivery_method", DELIVERY_METHOD, nullable=False),
        # The four money columns are the output of the shared money functions, frozen at creation.
        sa.Column("delivery_fee_santim", sa.BigInteger(), nullable=False),
        sa.Column("platform_fee_santim", sa.BigInteger(), nullable=False),
        sa.Column("vat_santim", sa.BigInteger(), nullable=False),
        sa.Column("total_santim", sa.BigInteger(), nullable=False),
        sa.Column("fee_payer", FEE_PAYER, nullable=False),
        sa.Column("buyer_address_woreda_id", sa.Integer(), nullable=False),
        sa.Column("buyer_address_text", sa.String(length=300), nullable=False),
        sa.Column("pickup_code", sa.String(length=6), nullable=False),
        sa.Column("status", ORDER_STATUS, nullable=False),
        # Where the order was before a dispute, so rejecting the dispute restores it. Null while
        # no dispute is open, and never inferred from `status`.
        sa.Column("state_before_dispute", ORDER_STATUS, nullable=True),
        sa.Column("funded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_weight_kg", sa.Integer(), nullable=True),
        sa.Column("cancel_reason", sa.String(length=40), nullable=True),
        sa.Column(
            "payment_mode",
            PAYMENT_MODE,
            server_default=PAYMENT_MODE.enums[0],
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
        sa.ForeignKeyConstraint(["buyer_id"], ["users.id"], name=op.f("fk_orders_buyer_id_users")),
        sa.ForeignKeyConstraint(
            ["listing_id"], ["listings.id"], name=op.f("fk_orders_listing_id_listings")
        ),
        sa.ForeignKeyConstraint(
            ["offer_id"], ["offers.id"], name=op.f("fk_orders_offer_id_offers")
        ),
        sa.ForeignKeyConstraint(
            ["seller_id"], ["users.id"], name=op.f("fk_orders_seller_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["buyer_address_woreda_id"],
            ["woredas.id"],
            name=op.f("fk_orders_buyer_address_woreda_id_woredas"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint("short_code", name=op.f("uq_orders_short_code")),
    )
    # `mine?as=buyer` and `mine?as=seller` are the only two reads of an order by party, and both
    # filter by status, so both are served by these pairs.
    op.create_index(op.f("ix_orders_buyer_id_status"), "orders", ["buyer_id", "status"])
    op.create_index(op.f("ix_orders_seller_id_status"), "orders", ["seller_id", "status"])
    # Cancellation returns weight to a listing, and the sweep that does it lazily walks orders by
    # status rather than by party.
    op.create_index(op.f("ix_orders_status_created_at"), "orders", ["status", "created_at"])

    op.create_table(
        "quantity_reservations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("listing_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("holder_type", HOLDER_TYPE, nullable=False),
        sa.Column("holder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity_kg", sa.Integer(), nullable=False),
        sa.Column("state", RESERVATION_STATE, nullable=False),
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
            ["holder_id"],
            ["orders.id"],
            name=op.f("fk_quantity_reservations_holder_id_orders"),
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_quantity_reservations_listing_id_listings"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quantity_reservations")),
    )
    # Availability is the sum of the blocking states for one listing. This is that pair exactly,
    # on the path where a slow query is a slow sale.
    op.create_index(
        op.f("ix_quantity_reservations_listing_id_state"),
        "quantity_reservations",
        ["listing_id", "state"],
    )
    # One order's weight is released or converted by holder lookup, and the blocking-order report
    # in an `ORDER_CONFLICT` is a lookup from an order back to what it holds.
    op.create_index(
        op.f("ix_quantity_reservations_holder_id"), "quantity_reservations", ["holder_id"]
    )

    # Shape rules a foreign key cannot state, held in the database as well as in the service so a
    # row written by anything else cannot be one the application would have refused.
    op.create_check_constraint(
        op.f("ck_orders_short_code_shape"),
        "orders",
        sa.text(f"short_code ~ '^GL-[{CROCKFORD_SIX}]{{6}}$'"),
    )
    op.create_check_constraint(
        op.f("ck_orders_pickup_code_digits"),
        "orders",
        sa.text("pickup_code ~ '^[0-9]{6}$'"),
    )
    op.create_check_constraint(
        op.f("ck_orders_quantity_kg_range"),
        "orders",
        sa.text("quantity_kg BETWEEN 1 AND 1000000"),
    )
    op.create_check_constraint(
        op.f("ck_orders_unit_price_santim_positive"),
        "orders",
        sa.text("unit_price_santim > 0"),
    )
    op.create_check_constraint(
        op.f("ck_orders_money_non_negative"),
        "orders",
        sa.text("delivery_fee_santim >= 0 AND platform_fee_santim >= 0 AND vat_santim >= 0"),
    )
    op.create_check_constraint(
        op.f("ck_orders_total_santim_positive"),
        "orders",
        sa.text("total_santim > 0"),
    )
    op.create_check_constraint(
        op.f("ck_orders_delivered_weight_kg_range"),
        "orders",
        sa.text("delivered_weight_kg IS NULL OR delivered_weight_kg BETWEEN 1 AND 1000000"),
    )
    # The parties are two sides of one trade, so the same account cannot be both.
    op.create_check_constraint(
        op.f("ck_orders_parties_differ"),
        "orders",
        sa.text("buyer_id <> seller_id"),
    )
    op.create_check_constraint(
        op.f("ck_orders_cancel_reason_only_when_cancelled"),
        "orders",
        sa.text("cancel_reason IS NULL OR status IN ('CANCELLED', 'CANCELLED_REFUNDED')"),
    )
    op.create_check_constraint(
        op.f("ck_quantity_reservations_quantity_kg_range"),
        "quantity_reservations",
        sa.text("quantity_kg BETWEEN 1 AND 1000000"),
    )


def downgrade() -> None:
    op.drop_table("quantity_reservations")
    op.drop_table("orders")
    bind = op.get_bind()
    for enum_type in reversed(NEW_ENUM_TYPES):
        enum_type.drop(bind, checkfirst=True)
