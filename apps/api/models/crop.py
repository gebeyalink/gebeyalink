"""The crops a listing may be written against.

A crop carries the unit its price is quoted in, because a price without a unit is not a
price. Most crops are weighed; a few are counted, and those are marked inactive and left
out of the weight-based lists until they are given a unit of their own.

The identifier is the specification's own number rather than a code of our own, so that a
row here and the same row in the published taxonomy are recognisably one thing.
"""

from __future__ import annotations

import enum

from sqlalchemy import Boolean, Integer, SmallInteger, String
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base
from apps.api.db.enums import stored_values


class CropCategory(enum.StrEnum):
    """What kind of thing a crop is, which is how the catalogue is grouped."""

    CEREALS = "CEREALS"
    PULSES = "PULSES"
    OILSEEDS = "OILSEEDS"
    FRUITS = "FRUITS"
    VEGETABLES = "VEGETABLES"
    COFFEE = "COFFEE"
    LIVESTOCK = "LIVESTOCK"
    OTHER = "OTHER"


class CropBaseUnit(enum.StrEnum):
    """The unit a price for this crop is quoted in."""

    KG = "KG"
    UNIT = "UNIT"


class Crop(Base):
    """One crop, named in each language, with the unit its prices use."""

    __tablename__ = "crops"

    id: Mapped[int] = mapped_column(SmallInteger(), primary_key=True, autoincrement=False)
    name_en: Mapped[str] = mapped_column(String(64), nullable=False)
    name_am: Mapped[str] = mapped_column(String(64), nullable=False)
    name_om: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name_ti: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[CropCategory] = mapped_column(
        SqlEnum(CropCategory, name="crop_category", values_callable=stored_values),
        nullable=False,
    )
    base_unit: Mapped[CropBaseUnit] = mapped_column(
        SqlEnum(
            CropBaseUnit,
            name="crop_base_unit",
            values_callable=stored_values,
            server_default="KG",
        ),
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(Boolean(), server_default="true", nullable=False)
    # How long an escrow on this crop is held before it can be released without a person
    # releasing it. Grain keeps far longer than leafy produce, so it is held per crop
    # rather than as one platform-wide window.
    escrow_hold_hours: Mapped[int] = mapped_column(Integer(), server_default="72", nullable=False)
