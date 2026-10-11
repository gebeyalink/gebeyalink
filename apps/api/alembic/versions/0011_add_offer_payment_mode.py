"""offers.payment_mode

An offer chooses how the sale it would become is paid for, and the choice is made when the
thread opens. This column holds that choice on every row of the thread, so a counter carries
what the offer it answers already said rather than restating it, and so an accepted offer can
hand the mode to the order it becomes without reading configuration that may since have changed.

Two things are worth stating rather than leaving to be inferred.

    The enum type already exists. ``payment_mode`` was created with the orders table, and this
    column reuses it — ``create_type=False`` is what stops this migration asking for a type that
    is already there — rather than declaring a second vocabulary of the same two words.

    Every offer that exists today predates the choice, and every one of them was an escrow offer:
    COD did not exist when they were placed. The server default records exactly what those rows
    already were, so there is nothing to backfill beyond creating the column with it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The mode enum came into existence with the orders table, so it is used rather than created.
#: `create_type=False` on the column is what stops this migration asking for a type that is
#: already there.
PAYMENT_MODE = postgresql.ENUM("ESCROW", "COD", name="payment_mode", create_type=False)


def upgrade() -> None:
    op.add_column(
        "offers",
        sa.Column(
            "payment_mode",
            PAYMENT_MODE,
            server_default=PAYMENT_MODE.enums[0],
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("offers", "payment_mode")
