"""The fee policy an order is charged under, kept as dated rows rather than one setting.

A fee is money, and money needs a history. If the platform's percentage were a single mutable
row, changing it would rewrite what every earlier order appeared to have been charged under;
the orders themselves are frozen, but a report joining them to the current policy would lie.
Rows with an ``effective_from`` and an ``effective_to`` make the change an event: the old row is
closed at the moment the new one opens, and a reader at any instant finds exactly one active
policy.

The values are stored as integers. Percentages are basis points (``200`` is two percent,
``1500`` is fifteen percent), and the bounds are santim. That keeps the configuration in the
same exact-integer vocabulary as the arithmetic that consumes it.

``created_by`` deliberately carries no foreign key. A fee row is financial history and must
outlive staff-table maintenance; a foreign key would also make ``TRUNCATE staff_users CASCADE``
take the active policy with it. The column names the approving staff member for the record, and
the audit log is the authority on that approval.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, text
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base, TimestampMixin
from apps.api.db.enums import stored_values
from packages.shared.money import FeePayer


class FeeConfig(TimestampMixin, Base):
    """One dated fee policy."""

    __tablename__ = "fee_config"
    __table_args__ = (
        CheckConstraint("platform_fee_bps > 0", name="platform_fee_bps_positive"),
        CheckConstraint(
            "vat_bps >= 0 AND vat_bps <= 10000",
            name="vat_bps_range",
        ),
        CheckConstraint("min_fee_santim >= 0", name="min_fee_santim_non_negative"),
        CheckConstraint(
            "max_fee_santim >= min_fee_santim",
            name="max_fee_santim_not_below_minimum",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="effective_window_ordered",
        ),
        Index(
            "uq_fee_config_one_active",
            "effective_to",
            unique=True,
            postgresql_where=text("effective_to IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    fee_payer: Mapped[FeePayer] = mapped_column(
        SqlEnum(FeePayer, name="fee_payer", values_callable=stored_values),
        nullable=False,
    )
    platform_fee_bps: Mapped[int] = mapped_column(Integer(), nullable=False)
    min_fee_santim: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    max_fee_santim: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    vat_bps: Mapped[int] = mapped_column(Integer(), nullable=False)

    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    # Null means this is the policy in force right now. The partial unique index above allows
    # exactly one such row, so "the current fee" can never be two answers.
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # No foreign key; see the module docstring. Null for the migration's seeded policy.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )

    # A short note explaining a manual change, kept beside the values it explains. The audit
    # row carries the hash; this is the human-readable reason the editor collected.
    change_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
