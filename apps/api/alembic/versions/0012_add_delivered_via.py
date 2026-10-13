"""orders.delivered_via

Every road into ``DELIVERED`` - the buyer's confirmation, the seller breaking the pickup code,
the pickup window closing, the shipment window closing - passes through one settlement chokepoint
and leaves the same trace: the status, the timestamp, and (for the pickup roads) a spent code.
Nothing on the row says which mechanism delivered the order.

Two things are worth stating rather than leaving to be inferred.

    The value is not derivable later. A reader could compare ``delivered_at`` against ``ready_at``
    plus the pickup constant, or ``shipped_at`` plus the crop's holding hours - but the holding
    hours are editable after the sale and the pickup window is a code constant, so that comparison
    asks today's boundary about yesterday's delivery and can answer differently each time it is
    run. A fact that can change under a reader is not a fact. The column records it at the moment
    it happens.

    It is nullable with no server default, and that is deliberate. The four values are a
    vocabulary, not a backfill: an order delivered before this column existed has no mechanism
    recorded, and computing one now would be inventing history. ``NULL`` is the honest answer for
    those rows and for every order that has not been delivered yet.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The vocabulary is created by this migration, so ``create_type=False`` on the column is what
#: keeps SQLAlchemy from asking for a type this migration is about to create itself, and the
#: downgrade drops exactly the type it created.
DELIVERED_VIA = postgresql.ENUM(
    "BUYER_CONFIRMED",
    "PICKUP_CODE",
    "PICKUP_WINDOW",
    "SHIPMENT_WINDOW",
    name="delivered_via",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    # Created explicitly, before the column that uses it, and dropped only by the downgrade that
    # created it: a type created here is this migration's to remove.
    DELIVERED_VIA.create(bind, checkfirst=True)
    op.add_column("orders", sa.Column("delivered_via", DELIVERED_VIA, nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "delivered_via")
    bind = op.get_bind()
    DELIVERED_VIA.drop(bind, checkfirst=True)
