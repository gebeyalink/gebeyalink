"""order fulfillment columns

A seller marks an order ready for pickup or shipped (FR-605, FR-701, FR-702), and a buyer
confirms delivery, tolerance permitting (FR-703, FR-2507). Two changes to the schema carry
that:

    `ready_at` is when the seller said the goods were waiting to be collected. It is new
    because the pickup auto-confirm window (FR-605) has to be measured from somewhere, and the
    moment an order became `READY_FOR_PICKUP` is the only honest starting point - the seller is
    not late to hand over goods they never said were ready.

    `pickup_code` becomes nullable because it is now consumed. The code is the seller's proof
    that the buyer was handed over the goods: it is generated at creation, shown to the buyer,
    and read back by the seller at the roadside. The instant a pickup is confirmed the code's
    work is done, and a code that has been spent still sitting on the row would let the same
    six digits authorise a second handover. Nullabillity is the consumption.

Neither column is written by anything other than the fulfillment slice, which is to say both
are null on the vast majority of orders for the lifetime of those orders: `ready_at` until a
pickup is marked ready, `pickup_code`... never null before a code was ever used, because every
order is born with one. Null on `pickup_code` is meaningful rather than missing, so the column
stays nullable and nothing treats null as "no code was created".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column(
        "orders",
        "pickup_code",
        existing_type=sa.String(length=6),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "orders",
        "pickup_code",
        existing_type=sa.String(length=6),
        nullable=False,
    )
    op.drop_column("orders", "ready_at")
