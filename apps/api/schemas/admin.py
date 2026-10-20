"""Request and response bodies for the staff routes.

The staff surface is deliberately small and its bodies are too. Three kinds live here.

    The *sign-in* bodies: what a staff member sends to get in, to change their password, and
    nothing else. The response is a token and its lifetime — no staff profile, because the
    client that just proved a password does not need to be told who it is.

    The *staff administration* bodies: creating a staff account and disabling one. The creation
    response carries the provisioning URI exactly once, and no later response carries it at
    all — that is a fact the shape states rather than the handler remembering.

    The *queue* bodies: the KYC case as a reviewer reads it and the rejection reason they send
    back. The reviewer's view of a case is wider than the submitter's: it includes the document
    addresses, because reading them is the reviewer's whole job.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from apps.api.models.kyc_case import KycDocType, KycRejectionReason, KycStatus, KycTier
from apps.api.models.staff_user import StaffRole

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
