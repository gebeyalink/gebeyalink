"""Request and response bodies for the staff routes.

The staff surface is deliberately small and its bodies are too. Four kinds live here.

    The *sign-in* bodies: what a staff member sends to get in, to change their password, and
    nothing else. The response is a token and its lifetime — no staff profile, because the
    client that just proved a password does not need to be told who it is.

    The *staff administration* bodies: creating a staff account and disabling one. The creation
    response carries the provisioning URI exactly once, and no later response carries it at
    all — that is a fact the shape states rather than the handler remembering.

    The *queue* bodies: the KYC case as a reviewer reads it and the rejection reason they send
    back. The reviewer's view of a case is wider than the submitter's: it includes the document
    addresses, because reading them is the reviewer's whole job.

    The *operator* bodies: a trader account and the few things staff may change about one, a
    listing's moderation reason, and a row of the append-only trail. They are separate from the
    queue shapes because the console reads an account, not a case, and because an audit row is
    evidence rather than a request.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from apps.api.models.kyc_case import KycDocType, KycRejectionReason, KycStatus, KycTier
from apps.api.models.listing import ListingStatus
from apps.api.models.staff_user import StaffRole
from apps.api.models.user import AccountStatus, PrimaryRole, VerificationTier
from apps.api.models.user_role import Role
from packages.shared.i18n.languages import Language

#: The ceiling on an email address, per RFC 5321. Also the column's width, stated once here so
#: the shape and the schema agree.
MAX_EMAIL_LENGTH = 320


def _is_email(value: str) -> bool:
    """A light shape check: one ``@``, text either side, no spaces.

    Not an RFC 5322 parser and not a deliverability check — the address is an identifier a
    staff member signs in with, and the operator who creates the account typed it. This refuses
    the obviously malformed without pulling an address-validation library into the credential
    path, which the slice's stdlib-only ruling forbids.
    """
    local, at, domain = value.partition("@")
    return bool(at) and bool(local) and "." in domain and " " not in value


class StaffLoginBody(BaseModel):
    """A staff member proving themselves: what they know, and what they have."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=MAX_EMAIL_LENGTH)
    password: str = Field(min_length=1)
    totp_code: str = Field(min_length=6, max_length=8)

    @field_validator("email")
    @classmethod
    def _email_looks_like_one(cls, value: str) -> str:
        if not _is_email(value):
            raise ValueError("must_be_an_email_address")
        return value.lower()


class StaffTokenResponse(BaseModel):
    """The token a signed-in staff member carries, and how long it lasts.

    ``scope`` is stated rather than implied: a token minted on an account that must change its
    password can do only that, and reading the scope is how a client learns it is in that state
    without trying another route and being refused.
    """

    model_config = ConfigDict(extra="forbid")

    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    scope: str


class StaffPasswordChangeBody(BaseModel):
    """A staff member choosing their own password, proved by the old one and a code."""

    model_config = ConfigDict(extra="forbid")

    old_password: str = Field(min_length=1)
    new_password: str = Field(
        min_length=12,
        description="SEC-12's floor: a staff password is at least twelve characters.",
    )
    totp_code: str = Field(min_length=6, max_length=8)


class StaffCreateBody(BaseModel):
    """An admin creating a staff account.

    ``password`` is optional: omitted, the server draws a strong one and returns it in the
    response for the admin to hand over; supplied, the admin's choice is used. Either way the
    new account must change it at first sign-in.
    """

    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=MAX_EMAIL_LENGTH)
    role: StaffRole
    password: str | None = Field(default=None, min_length=12)

    @field_validator("email")
    @classmethod
    def _email_looks_like_one(cls, value: str) -> str:
        if not _is_email(value):
            raise ValueError("must_be_an_email_address")
        return value.lower()


class StaffCreatedResponse(BaseModel):
    """The one response that carries a provisioning URI, plus the password if we drew it."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    email: str
    role: StaffRole
    must_change_password: bool
    #: Shown once and never again: the operator scans it into the new staff member's
    #: authenticator. No other response and no read route returns it.
    provisioning_uri: str
    #: Present only when the server generated the password because the admin did not supply one.
    generated_password: str | None = None


class StaffResponse(BaseModel):
    """A staff account as it may be read back: no secret, no URI."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    email: str
    role: StaffRole
    must_change_password: bool
    created_at: datetime
    last_login_at: datetime | None = None
    disabled_at: datetime | None = None


class StaffDisableBody(BaseModel):
    """The one mutation ``PATCH /admin/staff/{id}`` performs."""

    model_config = ConfigDict(extra="forbid")

    disabled: bool


class StaffRoleChangeBody(BaseModel):
    """An admin moving a staff member to a new role."""

    model_config = ConfigDict(extra="forbid")

    role: StaffRole


class KycCaseAdminResponse(BaseModel):
    """A KYC case as a reviewer reads it, documents included."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    user_id: UUID
    tier_requested: KycTier
    doc_type: KycDocType
    doc_images: list[str]
    status: KycStatus
    reviewer_id: UUID | None = None
    rejection_reason: KycRejectionReason | None = None
    created_at: datetime
    updated_at: datetime


class KycRejectionBody(BaseModel):
    """A reviewer's refusal, with §10.7's reason code."""

    model_config = ConfigDict(extra="forbid")

    reason_code: KycRejectionReason


class AgentApproveBody(BaseModel):
    """The account an admin is approving as an agent, identified by its user id."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID


class AgentDecisionResponse(BaseModel):
    """The outcome of an agent approval or revocation."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    agent: bool
    assignments_ended: int = 0


class AdminUserResponse(BaseModel):
    """A trader account as the staff console reads it.

    The primary role and the additional roles are shown separately because they answer
    different questions: the first is the label a journey is offered under, and the second is
    what else the account may do. Flattening them into one list would make an agent look like a
    primary label nobody chose.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    phone_e164: str
    role: PrimaryRole
    language: Language
    kyc_tier: VerificationTier
    status: AccountStatus
    additional_roles: list[Role]
    created_at: datetime
    updated_at: datetime


class AdminUserPatchBody(BaseModel):
    """The three mutations the staff console may perform on a trader account.

    ``reason`` is required when the status becomes ``FROZEN``. A ban without a stated reason is
    indistinguishable from an accident, and §11.1 asks for the reason to be part of the audited
    record. The field is accepted on every status change so an operator may explain a suspension
    as well; it is only mandatory for a freeze.
    """

    model_config = ConfigDict(extra="forbid")

    status: AccountStatus | None = None
    role: PrimaryRole | None = None
    additional_roles_add: list[Role] = Field(default_factory=list)
    additional_roles_remove: list[Role] = Field(default_factory=list)
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("status")
    @classmethod
    def _status_is_admin_settable(cls, value: AccountStatus | None) -> AccountStatus | None:
        allowed = {AccountStatus.ACTIVE, AccountStatus.FROZEN, AccountStatus.SUSPENDED}
        if value is not None and value not in allowed:
            raise ValueError("must_be_ACTIVE_FROZEN_or_SUSPENDED")
        return value


class ListingModerationBody(BaseModel):
    """Why a moderator is taking a listing off the board."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason_is_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("reason_must_not_be_blank")
        return cleaned


class ListingModerationResponse(BaseModel):
    """The listing's identity and the state the moderation move left it in."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    status: ListingStatus


class ListingAdminResponse(BaseModel):
    """A listing as the moderation screen reads it.

    This is the row a moderator picks from before suspending or restoring, so it carries the
    seller's identity as a UUID and no phone number: support finds the seller through the
    account search, and the listing row only needs to point at the account it came from.
    ``available_kg`` is what is left after the weight already consumed against the listing;
    the moderation screen does not count live reservations, which belong to the buyer-facing
    listing read.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    seller_id: UUID
    crop_id: int
    quantity_kg: int
    available_kg: int
    unit_price_santim: int
    status: ListingStatus
    region_code: str
    short_code: str
    created_at: datetime


class AuditLogResponse(BaseModel):
    """One row of the append-only staff-action trail, as search reads it back."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    actor_id: UUID | None
    actor_role: str
    action: str
    target_type: str
    target_id: UUID | None
    before_hash: str | None
    after_hash: str | None
    ip: str | None
    at: datetime


class AdminDashboardResponse(BaseModel):
    """The seven §3.3 metrics, including the ones the COD pilot cannot compute.

    A metric this dashboard cannot see is returned as null with a note rather than as zero:
    zero is a measurement, and "the pilot has no funded orders to measure" is not the same
    fact. The fields that are genuinely zero — GMV during a COD-only pilot — are returned as
    zero and explained by the client, because their arithmetic is real.
    """

    model_config = ConfigDict(extra="forbid")

    gmv_santim: int
    cod_gmv_santim: int
    completed_orders: int
    active_farmers: int
    offer_to_order_conversion: float | None
    offer_to_order_conversion_note: str
    cod_offer_to_order_conversion: float | None
    dispute_rate: float | None
    dispute_rate_note: str
    nps: float | None
    nps_surveyed_at: date | None
    nps_note: str | None = None
    ussd_share: float
    as_of: datetime


class OrderLedgerEntryResponse(BaseModel):
    """One side of a balanced journal, as the order-ledger browser reads it.

    The ledger tables are not part of the COD pilot, so this shape is what a funded order's
    row will look like when one exists. An empty ``entries`` list is the honest answer today.
    """

    model_config = ConfigDict(extra="forbid")

    journal_id: UUID
    journal_kind: str
    memo: str | None = None
    account_code: str
    entry_type: str
    amount_santim: int


class OrderLedgerResponse(BaseModel):
    """An order's ledger, or an empty ledger with the reason it is empty."""

    model_config = ConfigDict(extra="forbid")

    order_id: UUID
    entries: list[OrderLedgerEntryResponse] = Field(default_factory=list)
    note: str | None = None


class PriceOverrideBody(BaseModel):
    """An admin replacing the board's computed price for one crop, region and day."""

    model_config = ConfigDict(extra="forbid")

    price_santim: int = Field(
        gt=0,
        description="The override price in santim; the board's price is never negative or zero.",
    )
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason_is_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("reason_must_not_be_blank")
        return cleaned


class PriceOverrideResponse(BaseModel):
    """The board row the override wrote."""

    model_config = ConfigDict(extra="forbid")

    crop_id: int
    region_code: str
    day: date
    price_santim: int
