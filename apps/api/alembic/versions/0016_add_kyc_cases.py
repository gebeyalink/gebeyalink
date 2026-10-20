"""the kyc_cases table: a tier upgrade a trader has asked for

§11.1 names ``kyc_cases`` as the row between an account that wants a higher tier and the
verifier who decides. This migration creates it, with the columns §11.1 lists — ``user_id``,
``tier_requested``, ``doc_type``, ``doc_images``, ``status``, ``reviewer_id``,
``rejection_reason`` — and the row identity and two timestamps every table in the platform
carries.

Four enum types are created here and dropped only by this migration's downgrade, so a value a
reader cannot interpret cannot reach the table: ``kyc_tier`` (the two tiers a case may ask
for), ``kyc_doc_type`` (§11.1's four documents), ``kyc_status`` (the case's own life), and
``kyc_rejection_reason`` (§10.7's five refusal codes).

One column is deliberately written without a constraint it will eventually carry:

    ``reviewer_id`` names a *staff* account — §5.2's VERIFIER, SUPPORT, MODERATOR or ADMIN —
    and §5.2 is explicit that staff are "separate staff accounts, never shared with user
    accounts". No staff table exists in this schema yet, so there is no key for this column to
    reference and it is written as a plain nullable uuid. The admin slice that creates staff
    accounts adds the foreign key in its own migration. Writing a key to ``users.id`` here
    would claim the decision was made by a trader, which is exactly the fact §5.2 separates.

``doc_images`` is JSONB holding URIs rather than uploaded bytes, and defaults to an empty
list rather than to null: uploading and hosting a document image is the media flow's business
(ADR-0006, a later slice), so what a submission carries today is the list of addresses the
client's own storage handed back. This is a disclosed pilot limitation, not a property of the
column's type.

``user_id`` references ``users.id`` with no cascade, as every account reference in the
platform does: accounts are closed rather than deleted, so there is nothing for a cascade to
do.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The tiers a case may ask for. §6's ladder has a third rung, ``TIER_0``, which is where
#: every account starts and is therefore not something a case asks *for*; the service refuses
#: a request for the tier already held rather than storing one.
KYC_TIER = postgresql.ENUM(
    "TIER_1",
    "TIER_2",
    name="kyc_tier",
    create_type=False,
)

#: The identity documents a case may cite, §11.1's list unchanged.
KYC_DOC_TYPE = postgresql.ENUM(
    "FAYDA",
    "PASSPORT",
    "DRIVERS_LICENSE",
    "BUSINESS_LICENSE",
    name="kyc_doc_type",
    create_type=False,
)

#: Where a case sits in its own life. ``PENDING`` is the only state this slice writes;
#: ``APPROVED`` and ``REJECTED`` are the admin queue's two decisions, carried in the vocabulary
#: so the column and its readers agree on the whole list.
KYC_STATUS = postgresql.ENUM(
    "PENDING",
    "APPROVED",
    "REJECTED",
    name="kyc_status",
    create_type=False,
)

#: §10.7's refusal codes. No producer in this slice; the reject path writes them.
KYC_REJECTION_REASON = postgresql.ENUM(
    "DOC_BLURRY",
    "DOC_MISMATCH",
    "DOC_EXPIRED",
    "DOC_FRAUD_SUSPECTED",
    "OTHER",
    name="kyc_rejection_reason",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    # Created explicitly, before the table that uses them, and dropped only by the downgrade
    # that created them: a type created here is this migration's to remove.
    KYC_TIER.create(bind, checkfirst=True)
    KYC_DOC_TYPE.create(bind, checkfirst=True)
    KYC_STATUS.create(bind, checkfirst=True)
    KYC_REJECTION_REASON.create(bind, checkfirst=True)

    op.create_table(
        "kyc_cases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tier_requested", KYC_TIER, nullable=False),
        sa.Column("doc_type", KYC_DOC_TYPE, nullable=False),
        sa.Column(
            "doc_images",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            KYC_STATUS,
            server_default=KYC_STATUS.enums[0],
            nullable=False,
        ),
        # No foreign key: the reviewer is a staff account, and staff are a separate table
        # (§5.2) this schema does not hold yet. The admin slice adds the constraint.
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rejection_reason", KYC_REJECTION_REASON, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_kyc_cases_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kyc_cases")),
    )

    # The two read paths the table exists for: a verifier listing what still waits, and an
    # account reading its own cases. A PENDING submission is also refused by looking for an
    # existing PENDING case, which this index serves.
    op.create_index("ix_kyc_cases_user_id", "kyc_cases", ["user_id"], unique=False)
    op.create_index("ix_kyc_cases_status", "kyc_cases", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_kyc_cases_status", table_name="kyc_cases")
    op.drop_index("ix_kyc_cases_user_id", table_name="kyc_cases")
    op.drop_table("kyc_cases")
    bind = op.get_bind()
    # Dropped in the reverse of the order they were created; each is this migration's to
    # remove because this migration is what created it.
    KYC_REJECTION_REASON.drop(bind, checkfirst=True)
    KYC_STATUS.drop(bind, checkfirst=True)
    KYC_DOC_TYPE.drop(bind, checkfirst=True)
    KYC_TIER.drop(bind, checkfirst=True)
