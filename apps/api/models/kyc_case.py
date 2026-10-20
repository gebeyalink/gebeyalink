"""A tier upgrade a trader has asked for, waiting for a verifier to decide.

§6 governs who may trade how much, and the amount tracks ``users.kyc_tier`` rather than the
label on the account. Moving a tier is therefore two separate acts with two separate actors:
the account holder *submits* evidence and asks for a tier, and a verifier *reviews* that
evidence and decides. This table is the first act's row. The second act — approve or reject —
is the admin queue's, and nothing here moves a tier.

``status`` is the case's own life, not the trader's: ``PENDING`` while it waits,
``APPROVED`` or ``REJECTED`` once a verifier has ruled. A submission never writes a tier,
which is why a ``PENDING`` row and an unchanged ``users.kyc_tier`` go together by design.

Three choices are worth stating rather than leaving to be inferred.

    ``reviewer_id`` has no foreign key yet. The reviewing column names a *staff* account, and
    staff accounts (§5.2's VERIFIER, SUPPORT, MODERATOR, ADMIN) are a separate table from the
    trader accounts this schema holds — §5.2 says staff are "separate staff accounts, never
    shared with user accounts". That table does not exist in this schema yet, so the column is
    written without a foreign key and the admin slice that adds staff accounts adds the
    constraint. It is nullable because a ``PENDING`` case has no reviewer.

    ``doc_images`` holds URIs, not bytes. Uploading and hosting a document image is the
    media flow's business (ADR-0006, a later slice); what a submission carries today is the
    list of addresses the client's own photo storage handed back. This is a disclosed pilot
    limitation, recorded in the slice's report, not an accident of the column's type.

    ``tier_requested`` and ``doc_type`` are strict vocabularies the database enforces, not
    free text. A case asking for a tier nobody defined, or citing a document kind nobody
    accepts, is refused at the column rather than stored for a reviewer to puzzle over.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import Enum as SqlEnum
from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base, TimestampMixin
from apps.api.db.enums import stored_values


class KycTier(enum.StrEnum):
    """The tiers a trader may ask to be moved to.

    §6's ladder has three rungs; ``TIER_0`` is where every account starts and is not something
    a case asks *for*, so the requestable set is the two above it. A submission for the tier
    the account already holds is refused by the service rather than stored here, because the
    vocabulary and the rule are different things: this names what may be asked for, the
    service decides what a given asker may request.
    """

    TIER_1 = "TIER_1"
    TIER_2 = "TIER_2"


class KycDocType(enum.StrEnum):
    """The identity documents a case may cite.

    §11.1's list, unchanged. ``FAYDA`` is the national ID and the usual path to ``TIER_1``;
    the others are the accepted alternatives, and ``BUSINESS_LICENSE`` is the one a business
    account submits — the submission that also carries a tax number.
    """

    FAYDA = "FAYDA"
    PASSPORT = "PASSPORT"
    DRIVERS_LICENSE = "DRIVERS_LICENSE"
    BUSINESS_LICENSE = "BUSINESS_LICENSE"


class KycStatus(enum.StrEnum):
    """Where a case sits in its own life.

    ``PENDING`` is the state every submission lands in and the only one this slice writes.
    ``APPROVED`` and ``REJECTED`` are the two a verifier's decision moves it to; the enum
    carries them so the column and the reader agree on the whole vocabulary, even though the
    producers for those two are the admin queue's.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class KycRejectionReason(enum.StrEnum):
    """Why a case was refused. §10.7's list, carried in full by the enum.

    No producer today: every value here is written by the reject path the admin queue owns.
    The vocabulary lives on the type so a reader never meets a word the database has not
    admitted, which is what an enum column is for.
    """

    DOC_BLURRY = "DOC_BLURRY"
    DOC_MISMATCH = "DOC_MISMATCH"
    DOC_EXPIRED = "DOC_EXPIRED"
    DOC_FRAUD_SUSPECTED = "DOC_FRAUD_SUSPECTED"
    OTHER = "OTHER"


class KycCase(TimestampMixin, Base):
    """One request to move a trader's tier, and the verifier's decision once there is one."""

    __tablename__ = "kyc_cases"
    __table_args__ = (
        # The queue's read is "the cases still waiting", and the account's read is "my cases".
        # Both are named here so a reader of the schema sees the two access paths the table
        # exists to serve rather than discovering them from whatever queries happen to exist.
        Index("ix_kyc_cases_user_id", "user_id"),
        Index("ix_kyc_cases_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # A plain foreign key, not a cascade: accounts are closed rather than deleted, so there
    # is nothing for a cascade to do and the constraint records that a case has an owner.
    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_kyc_cases_user_id_users"),
        nullable=False,
    )
    tier_requested: Mapped[KycTier] = mapped_column(
        SqlEnum(KycTier, name="kyc_tier", values_callable=stored_values), nullable=False
    )
    doc_type: Mapped[KycDocType] = mapped_column(
        SqlEnum(KycDocType, name="kyc_doc_type", values_callable=stored_values), nullable=False
    )
    # URIs, not bytes: the list of where the client's photo storage put the images. Stored as
    # JSON so the shape is a list of strings rather than a second table for at most three
    # addresses, and never null: an empty list is "no images yet", which is a real state.
    doc_images: Mapped[list[str]] = mapped_column(
        postgresql.JSONB(),
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    status: Mapped[KycStatus] = mapped_column(
        SqlEnum(KycStatus, name="kyc_status", values_callable=stored_values),
        nullable=False,
        default=KycStatus.PENDING,
        server_default=KycStatus.PENDING.value,
    )
    # Null while the case waits. Names a staff account, which is a table this schema does not
    # hold yet, so there is no foreign key; the admin slice adds it. See the module docstring.
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )
    # Null unless the case was rejected. §10.7's vocabulary, enforced by the enum type.
    rejection_reason: Mapped[KycRejectionReason | None] = mapped_column(
        SqlEnum(KycRejectionReason, name="kyc_rejection_reason", values_callable=stored_values),
        nullable=True,
    )


def case_images(case: KycCase) -> list[Any]:
    """The case's image addresses as a plain list.

    ``doc_images`` is JSONB, so what SQLAlchemy hands back is whatever the driver decoded;
    this narrows it to the list the column is written as, so a caller does not have to reason
    about a type the schema already fixed.
    """
    return list(case.doc_images)


#: Re-exported so a router or a test naming a case's status does not have to reach into the
#: module that defines it and the migration that creates the type separately.
KYC_STATUS_VALUES = tuple(member.value for member in KycStatus)

__all__ = [
    "KYC_STATUS_VALUES",
    "KycCase",
    "KycDocType",
    "KycRejectionReason",
    "KycStatus",
    "KycTier",
    "case_images",
]
