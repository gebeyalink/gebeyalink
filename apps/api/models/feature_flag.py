"""The switches that decide which payment modes and pilots a deployment offers.

A feature flag is not configuration in the same sense as a database URL. It is an operational
decision that may need to change while the platform is running, and for the flags that guard
money it needs the same second pair of eyes as a fee change. Keeping it in a table rather than
a process environment variable is what makes "change the switch" an audited event instead of a
deploy.

Three columns carry the meaning. ``enabled`` is the master switch; nothing below it can turn a
flag on. ``percentage_rollout`` is an optional percentage of users, and ``region_targeting`` is
an optional list of region codes. A null in either of the optional columns means that dimension
does not constrain the flag; when both are null, an enabled flag is on for everyone. A missing
row is different from a disabled row: it is a deployment that has not installed the flag, and
readers refuse rather than guess.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, CheckConstraint, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base, TimestampMixin


class FeatureFlag(TimestampMixin, Base):
    """One named switch, with the two optional scopes it may be limited to."""

    __tablename__ = "feature_flags"
    __table_args__ = (
        UniqueConstraint("name", name="uq_feature_flags_name"),
        CheckConstraint(
            "percentage_rollout IS NULL "
            "OR (percentage_rollout >= 0 AND percentage_rollout <= 100)",
            name="percentage_rollout_range",
        ),
        CheckConstraint(
            "region_targeting IS NULL OR cardinality(region_targeting) >= 1",
            name="region_targeting_not_empty",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text(), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean(), server_default=text("false"), nullable=False)
    # Null means "no percentage rollout". Zero is a real value that enables nobody, which is
    # why the percentage is nullable rather than defaulted to zero.
    percentage_rollout: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    # Null means "no region restriction". An empty list is refused by the check constraint so
    # that an operator cannot accidentally configure a flag that is on nowhere.
    region_targeting: Mapped[list[str] | None] = mapped_column(ARRAY(Text()), nullable=True)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
