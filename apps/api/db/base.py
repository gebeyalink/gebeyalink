"""Declarative base and shared column conventions.

Every table in the platform is declared against one metadata object, which is what the
migration tooling reads to compare the live database against the models. Keeping a single
naming convention on the base means a column called ``created_at`` means the same thing
in every table.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Explicit constraint names keep migrations readable and let a rollback target exactly
#: one constraint instead of guessing from a generated name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Common ancestor for every mapped class."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Rows record when they were written, in UTC, by the database clock.

    The database is the clock rather than the application so that rows written by a
    migration, a backfill, and a live request all agree on ordering.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
