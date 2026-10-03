"""Conversions from a trader's unit into the kilogram a listing is priced in.

Traders do not all weigh in the same unit. This table says what one local unit comes to in
kilograms for a given crop, so a price can be compared across the market without anybody
guessing. Only conversions backed by a real source belong here; a missing conversion is a
missing answer, not an invitation to estimate.
"""

from __future__ import annotations

import enum
from decimal import Decimal

from sqlalchemy import Enum as SqlEnum
from sqlalchemy import ForeignKey, Numeric, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base
from apps.api.db.enums import stored_values


class CropUnitCode(enum.StrEnum):
    """The units a price may be quoted in besides the kilogram."""

    QUINTAL = "QUINTAL"
    SACK = "SACK"
    JONIYA = "JONIYA"


class CropUnit(Base):
    """How many kilograms one unit of a crop weighs."""

    __tablename__ = "crop_units"

    crop_id: Mapped[int] = mapped_column(SmallInteger(), ForeignKey("crops.id"), primary_key=True)
    unit_code: Mapped[CropUnitCode] = mapped_column(
        SqlEnum(CropUnitCode, name="crop_unit_code", values_callable=stored_values),
        primary_key=True,
    )
    kg_equivalent: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
