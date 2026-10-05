"""offers

Transcribed from the core-entity definition in the specification:

    **offers** - `listing_id fk`, `buyer_id fk`, `thread_id uuid`, `thread_depth smallint
    default 0`, `quantity_kg int`, `price_santim bigint`, `valid_until timestamptz`, `status
    offer_status not null`, `version int`. Index `(listing_id, status)`.

The preamble covers the rest: `id uuid PK default gen_random_uuid()`, `created_at` and
`updated_at` from the database clock, and `version int not null default 0` because an offer is
a mutable business entity.

Three choices are worth naming, because each is a decision the column list leaves open.

`thread_id` is `not null`. The specification does not say, and the nullable reading — an
original with no thread and counters carrying one — would mean "which thread is this in" has two
answers depending on which row you asked. Every offer belongs to exactly one thread, including
the one that starts it, so the original generates the value it will later be found by.

`buyer_id` does not change along a thread. A seller's counter is still a counter *on the buyer's
negotiation*: it names who the negotiation is with, not who is speaking in it. Who is speaking
is `thread_depth`'s job, which is why there is no proposer column to disagree with it. The
alternative — recording the seller as the "buyer" of their own counter, so that a single column
always named the offer's author — would have made `withdraw` mean something different halfway
down a thread, and FR-506 gives that word to the buyer.

The `(listing_id, status)` index is the specification's, and it is the right pair for both
access paths this table has: the seller reading the offers on their listing filtered by state,
and the cap on how many open offers a listing may carry, which is a count over exactly that
pair. Nothing reads offers by thread in the request path — a thread is always reached through
its listing — so there is deliberately no index on `thread_id`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OFFER_STATUS = sa.Enum(
    "OPEN",
    "ACCEPTED",
    "REJECTED",
    "WITHDRAWN",
    "EXPIRED",
    "SUPERSEDED",
    name="offer_status",
)


def upgrade() -> None:
    # The enum type comes into existence with the table that holds it. Creating it separately
    # would ask for the same type twice and be refused the second time.
    op.create_table(
        "offers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("listing_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("buyer_id", postgresql.UUID(as_uuid=True), nullable=False),
        # The thread an offer belongs to. An original generates its own, so that every row in a
        # negotiation is found by the same question whatever its position in it.
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=False),
        # How many answers deep this is. Zero is a buyer's opening bid, one is the seller's
        # answer, and the parity of the number is what says whose terms these are.
        sa.Column("thread_depth", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("quantity_kg", sa.Integer(), nullable=False),
        # An integer, never a float: money is not held in anything that can round.
        sa.Column("price_santim", sa.BigInteger(), nullable=False),
        # When these terms stop being on the table. Read against the clock on every read, so an
        # offer nobody acted on lapses without waiting for a job to say so.
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", OFFER_STATUS, nullable=False),
        # Carried because §11.1 gives every mutable business entity one. Nothing here edits an
        # offer's terms: a change of terms is a new offer, which is what a counter is.
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
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_offers_listing_id_listings"),
        ),
        sa.ForeignKeyConstraint(
            ["buyer_id"],
            ["users.id"],
            name=op.f("fk_offers_buyer_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offers")),
    )
    # The specification's index, and the right pair for both paths this table is read by: the
    # seller reading the offers on their listing, and the cap on how many open offers a listing
    # may carry, which is a count over exactly this pair.
    op.create_index(op.f("ix_offers_listing_id_status"), "offers", ["listing_id", "status"])
    # The buyer reads their own offers to see which of their bids are still open, which is a
    # scan by buyer rather than by listing.
    op.create_index(op.f("ix_offers_buyer_id"), "offers", ["buyer_id"])

    # The weight and money rules are checks on the shape of what was sent, which is what a
    # foreign key cannot say. They are held here as well as in the service so that a row
    # written by anything else cannot be one the application would have refused.
    op.create_check_constraint(
        op.f("ck_offers_quantity_kg_range"),
        "offers",
        sa.text("quantity_kg BETWEEN 1 AND 1000000"),
    )
    op.create_check_constraint(
        op.f("ck_offers_price_santim_positive"),
        "offers",
        sa.text("price_santim > 0"),
    )
    # A depth outside this range cannot be produced by the service, and a row claiming to be a
    # hundred answers deep would read as the buyer's terms on parity grounds alone.
    op.create_check_constraint(
        op.f("ck_offers_thread_depth_range"),
        "offers",
        sa.text("thread_depth BETWEEN 0 AND 3"),
    )


def downgrade() -> None:
    op.drop_table("offers")
    OFFER_STATUS.drop(op.get_bind(), checkfirst=True)
