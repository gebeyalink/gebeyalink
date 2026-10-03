"""The account record.

A person arrives with a phone number and, once they have proved they can receive messages
on it, an account. Everything else about them is attached to that account later: what they
trade in, how far their verification has gone, which language they read.

The account deliberately holds no credentials. Sign-in is by verification code, so there
is no password column to leak, reset, or phish.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base, TimestampMixin
from apps.api.db.enums import stored_values
from packages.shared.i18n.languages import Language


class PrimaryRole(enum.StrEnum):
    """The one activity an account is primarily known for.

    A person who both grows and buys carries both, but the marketplace needs a single
    label to decide which journeys to offer by default.
    """

    FARMER = "FARMER"
    BUYER = "BUYER"


class VerificationTier(enum.StrEnum):
    """How much a platform has verified about a person.

    Tier 0 is the starting point and requires nothing; higher tiers unlock larger
    transactions and faster settlement, so the level is recorded on the account rather
    than inferred at request time.
    """

    TIER_0 = "TIER_0"
    TIER_1 = "TIER_1"
    TIER_2 = "TIER_2"


class AccountStatus(enum.StrEnum):
    """Where an account stands with the platform.

    Only SUSPENDED and CLOSED stop a person transacting outright. RECOVERY and COOLDOWN are
    temporary states a person passes through on their way back to ACTIVE, which is why they
    are separate values rather than flags on an active account.
    """

    ACTIVE = "ACTIVE"
    RECOVERY = "RECOVERY"
    COOLDOWN = "COOLDOWN"
    FROZEN = "FROZEN"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"


class User(Base, TimestampMixin):
    """One person who can trade on the platform."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # The one identifier a trader has before they have an account, and the anchor that
    # makes a re-registration sign them back in rather than create a duplicate.
    phone_e164: Mapped[str] = mapped_column(String(13), unique=True, nullable=False, index=True)

    role: Mapped[PrimaryRole] = mapped_column(
        SqlEnum(
            PrimaryRole,
            name="primary_role",
            native_enum=False,
            length=8,
            values_callable=stored_values,
        ),
        nullable=False,
    )
    language: Mapped[Language] = mapped_column(
        SqlEnum(
            Language,
            name="language",
            native_enum=False,
            length=2,
            values_callable=stored_values,
        ),
        nullable=False,
        default=Language.AM,
        server_default=Language.AM.value,
    )
    kyc_tier: Mapped[VerificationTier] = mapped_column(
        SqlEnum(
            VerificationTier,
            name="verification_tier",
            native_enum=False,
            length=6,
            values_callable=stored_values,
        ),
        nullable=False,
        default=VerificationTier.TIER_0,
        server_default=VerificationTier.TIER_0.value,
    )
    status: Mapped[AccountStatus] = mapped_column(
        SqlEnum(
            AccountStatus,
            name="account_status",
            native_enum=False,
            length=9,
            values_callable=stored_values,
        ),
        nullable=False,
        default=AccountStatus.ACTIVE,
        server_default=AccountStatus.ACTIVE.value,
    )

    # Proof that the trader accepted the platform's terms, and when. Kept on the account
    # because a first listing or purchase is only allowed once this is filled in.
    terms_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    terms_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
