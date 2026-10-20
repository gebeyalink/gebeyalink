"""The published regional price averages a listing's price is read against.

One row is one crop, in one region, on one day, and what it holds is the average unit price
seen there that day, or an honest statement that there were not enough sales to say.

The distinction matters more than it sounds. An empty table and a row that says "not enough
data" are two different refusals to judge a price, and both must lead to the same place:
the listing is posted and simply not flagged. A price check that guessed an average from two
sales would mark good listings as odd far more often than it would catch a bad one.

This table is filled by the overnight aggregation and is read, never written, by the
creation rules. Nothing in the request path computes an average.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import BigInteger, Boolean, Date, ForeignKey, Integer, SmallInteger, String
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base, TimestampMixin


class PriceIndex(TimestampMixin, Base):
    """What a crop cost in a region on a day, or that we do not know."""

    __tablename__ = "price_indices"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    crop_id: Mapped[int] = mapped_column(
        SmallInteger(),
        ForeignKey("crops.id", name="fk_price_indices_crop_id_crops"),
        nullable=False,
    )
    region_code: Mapped[str] = mapped_column(
        String(length=2),
        ForeignKey("regions.code", name="fk_price_indices_region_code_regions"),
        nullable=False,
    )
    day: Mapped[date] = mapped_column(Date(), nullable=False)
    # Null when no average could be computed. A zero here would read as a price of nothing,
    # and every listing against it would look like a mistake.
    avg_unit_price_santim: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    # The edges of the same window the average came out of: the cheapest and the dearest
    # unit price anybody asked or paid in it. Null alongside a null average, because a
    # range of two points is not a range - it is two prices dressed as one - and the board
    # exists to not do that.
    min_unit_price_santim: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    max_unit_price_santim: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    # How many sales the average rests on, so a thin average can be recognised as thin
    # without re-reading the transactions.
    sample_size: Mapped[int] = mapped_column(Integer(), server_default="0", nullable=False)
    # True means "we do not know" rather than "we know it was zero". Defaults to true so a
    # row written without a judgement is read as declining to make one.
    insufficient_data: Mapped[bool] = mapped_column(
        Boolean(), server_default="true", nullable=False
    )
