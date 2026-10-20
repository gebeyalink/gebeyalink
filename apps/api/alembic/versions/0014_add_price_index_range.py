"""the price board's two edges

``price_indices`` has always held one number per (crop, region, day): the average the
overnight aggregation computed, a count of what it rested on, and a flag saying whether
that was enough to speak. The public contract publishes three numbers rather than one -
the index and the range it sits between - and this migration adds the two the table was
missing: ``min_unit_price_santim`` and ``max_unit_price_santim``.

Three choices are worth stating rather than leaving to be inferred.

    The edges persist rather than being recomputed. The range is a fact about the window
    that produced the index - the cheapest and dearest unit price anybody asked or paid in
    those thirty days - and a range recomputed later would be a range over whatever the
    tables still held then, which is a different statement wearing the same name.

    Both columns are nullable, and null means what the average's null has always meant
    here: the day declined to judge. A minimum of two observations is not a minimum, it is
    the two prices somebody happened to ask dressed as a range, so when the sample is thin
    all three figures are withheld together rather than the average alone.

    The names follow the column they stand beside (``avg_unit_price_santim``), not the
    names the HTTP contract gives the fields (``min_price_santim``). The table and the
    contract are two documents and neither borrows the other's vocabulary; the endpoint is
    where the two are translated.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "price_indices",
        sa.Column("min_unit_price_santim", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "price_indices",
        sa.Column("max_unit_price_santim", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("price_indices", "max_unit_price_santim")
    op.drop_column("price_indices", "min_unit_price_santim")
