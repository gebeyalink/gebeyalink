"""What a trader says about themselves.

The account says who someone is; the profile says what they trade as. It is created the
first time a trader fills something in, so an account that has only ever signed in has no
profile row yet, which is a normal state rather than a broken one.

The GPS point is deliberately write-only. A trader gives it once to help get their location
right; it is stored at about a metre's precision and is not published, not returned by any
endpoint, and not logged.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base, TimestampMixin
from apps.api.db.geo import LatitudeLongitude


class Profile(Base, TimestampMixin):
    """One trader's own details, alongside the account they signed in with."""

    __tablename__ = "profiles"
    __table_args__ = (
        # A region code is two characters, so a longer one is a malformed request rather than
        # an unknown region. Which of the thirteen it may be is the foreign key's business.
        CheckConstraint(
            "region_code IS NULL OR char_length(region_code) = 2",
            name="region_code_length",
        ),
        # The same rule the API enforces, held in the database as well so that anything
        # writing straight to the table cannot produce a name the API would have refused.
        CheckConstraint(
            "display_name IS NULL OR "
            "(char_length(display_name) BETWEEN 2 AND 60 "
            "AND btrim(display_name) = display_name)",
            name="display_name_length",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # One profile per account. The unique constraint is what makes "1:1" true in the
    # database rather than merely intended, so a second row cannot be written at all.
    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False
    )

    display_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    region_code: Mapped[str | None] = mapped_column(
        String(2), ForeignKey("regions.code"), nullable=True
    )
    woreda_id: Mapped[int | None] = mapped_column(
        Integer(), ForeignKey("woredas.id"), nullable=True
    )
    kebele: Mapped[str | None] = mapped_column(String(80), nullable=True)
    gps: Mapped[tuple[float, float] | None] = mapped_column(LatitudeLongitude(), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    business_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    license_no: Mapped[str | None] = mapped_column(Text(), nullable=True)
    tax_identification_number: Mapped[str | None] = mapped_column(Text(), nullable=True)

    # Bumped on every write. The offline client keeps its own copy and needs to know whether
    # what it holds is still what the server holds; `updated_at` says when, this says how
    # many times.
    version: Mapped[int] = mapped_column(Integer(), server_default="0", nullable=False)
