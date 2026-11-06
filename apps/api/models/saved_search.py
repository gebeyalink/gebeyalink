"""A search a trader asked the marketplace to keep for them.

A saved search is the search endpoint's filter set, remembered under an account so that the
platform can look for new matches while the trader is asleep. It is deliberately a copy of
the filters rather than a pointer at a query string: the rules about what each field means
live in the search service and its repository, and a saved search that stored text would have
to parse them again with a second, looser parser.

``last_digest_at`` is the clock the daily digest is measured from. It is null until the
first digest that mentions this search, and null means "measure from when the search was
saved" rather than "send everything ever posted". It is internal bookkeeping: the published
``SavedSearch`` contract carries the filters and the digest choice, not when we last wrote
to the trader about them.

``digest_channel`` reuses the notification channel vocabulary rather than inventing a
saved-search-only enum. The API admits only ``PUSH`` and ``SMS``, because those are the two
the contract publishes; the stored type has a third member for the in-app rail, and a
database constraint keeps this column out of it until the API grows a matching value.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base, TimestampMixin
from apps.api.db.enums import stored_values
from apps.api.models.listing import DeliveryOption
from apps.api.models.notification import NotificationChannel
from apps.api.models.user import VerificationTier


class SavedSearch(TimestampMixin, Base):
    """One filter set a trader chose to keep, and how its digest should be sent."""

    __tablename__ = "saved_searches"
    __table_args__ = (
        CheckConstraint(
            "region_code IS NULL OR char_length(region_code) = 2",
            name="region_code_length",
        ),
        CheckConstraint(
            "radius_km IS NULL OR radius_km BETWEEN 1 AND 500",
            name="radius_km_range",
        ),
        CheckConstraint(
            "seller_tier_min IS NULL OR seller_tier_min IN ('TIER_0', 'TIER_1', 'TIER_2')",
            name="seller_tier_min",
        ),
        CheckConstraint(
            "digest_channel IN ('PUSH', 'SMS')",
            name="digest_channel",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The account this search belongs to. Deleting the account takes its searches with it,
    # which is the same cascade every other row written on a trader's behalf follows.
    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The search filter set. Every field is nullable for the same reason it is optional on
    # the search endpoint: a null means "no opinion", not "an empty opinion".
    crop_id: Mapped[int | None] = mapped_column(
        SmallInteger(), ForeignKey("crops.id"), nullable=True
    )
    region_code: Mapped[str | None] = mapped_column(
        String(2), ForeignKey("regions.code"), nullable=True
    )
    woreda_id: Mapped[int | None] = mapped_column(
        Integer(), ForeignKey("woredas.id"), nullable=True
    )
    radius_km: Mapped[int | None] = mapped_column(SmallInteger(), nullable=True)
    price_min: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    price_max: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    harvest_date_from: Mapped[date | None] = mapped_column(Date(), nullable=True)
    harvest_date_to: Mapped[date | None] = mapped_column(Date(), nullable=True)
    organic: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    delivery_option: Mapped[DeliveryOption | None] = mapped_column(
        SqlEnum(DeliveryOption, name="delivery_option", values_callable=stored_values),
        nullable=True,
    )
    seller_tier_min: Mapped[VerificationTier | None] = mapped_column(
        SqlEnum(
            VerificationTier,
            name="verification_tier",
            native_enum=False,
            length=6,
            values_callable=stored_values,
        ),
        nullable=True,
    )

    # The digest decision travels with the search rather than in a separate settings row:
    # a trader may want a daily note about one search and silence about another.
    digest: Mapped[bool] = mapped_column(Boolean(), server_default="true", nullable=False)
    digest_channel: Mapped[NotificationChannel] = mapped_column(
        SqlEnum(NotificationChannel, name="notification_channel", values_callable=stored_values),
        server_default=NotificationChannel.SMS.value,
        nullable=False,
    )

    # Null until the first digest that names this search. Null therefore means "since the
    # search was saved", which is what lets a brand-new search get its first digest without
    # being flooded by everything already on the market.
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
