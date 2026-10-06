"""order confirmation columns

A cash-on-delivery order is confirmed by the two parties to it, one at a time (FR-2102), and
that confirmation is what moves an order out of `CREATED` — not money, of which a COD order
never sees any. `payment_mode` already exists for that, so the plan needed no column of its
own; but the confirmation itself has to be *written down* somewhere, and the three columns
below are the least of it:

    `confirmed_at` is when the second party confirmed and the order reached `CONFIRMED`. It is
    the moment from which the seller-delayed window is measured: the seller is not late until
    seventy-two hours after both parties agreed, because only then is there an agreement to be
    late on.

    `buyer_confirmed_at` and `seller_confirmed_at` are what make confirmation idempotent *per
    party*. Confirming is the buyer's move and then the seller's (or the other way round), and
    a party who confirms twice — a tap counted for missed responses, as every other mutation is —
    has to be told their first confirmation stands. Whether a party has confirmed is therefore a
    fact about the order, stored as the instant they did, and the second distinct confirmation
    is the one that sees both stamps and completes the move.

All three are null on every escrow order, which never confirms: an escrow order is confirmed by
the payment arriving (§10.2 gives `CREATED` an escrow-free life), and `funded_at` already
records that moment.

Lazy rather than stored on the read side: nothing here holds a flag that could disagree with
the clock. `seller_delayed` is derived from `confirmed_at` at read time by the same rule
`effective_status` uses for the funding window, so a reader who looks at an order the moment
its window closes is told the truth without a job having run.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("buyer_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("seller_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "seller_confirmed_at")
    op.drop_column("orders", "buyer_confirmed_at")
    op.drop_column("orders", "confirmed_at")
