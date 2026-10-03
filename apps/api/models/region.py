"""Ethiopian administrative regions.

A region is the widest geography the platform names: thirteen of them, each holding the
zones and woredas underneath. Seeded by migration, because regions are fixed by law rather
than chosen by us.

Only the English names are published, so the Amharic and Oromo columns stay empty until a
reviewed list exists to fill them.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base


class Region(Base):
    """A region and its name in each language a trader may read in."""

    __tablename__ = "regions"

    code: Mapped[str] = mapped_column(String(2), primary_key=True)
    name_en: Mapped[str] = mapped_column(String(64), nullable=False)
    name_am: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name_om: Mapped[str | None] = mapped_column(String(64), nullable=True)
