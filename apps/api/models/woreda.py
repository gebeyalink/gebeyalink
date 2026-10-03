"""The woredas a listing may name.

A woreda is the smallest geography the platform places a trader in. Its boundary is held as
real geometry so a point can be tested for membership, which is why the column exists even
though a boundary dataset has to be procured before anything is put in it.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base
from apps.api.db.geo import GeographyMultiPolygon


class Woreda(Base):
    """One woreda, its region, and its boundary when one is known."""

    __tablename__ = "woredas"

    id: Mapped[int] = mapped_column(Integer(), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    region_code: Mapped[str] = mapped_column(String(2), ForeignKey("regions.code"), nullable=False)
    zone: Mapped[str] = mapped_column(String(120), nullable=False)
    geog: Mapped[str | None] = mapped_column(GeographyMultiPolygon(), nullable=True)
