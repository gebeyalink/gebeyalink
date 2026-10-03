"""Request and response bodies for signing in.

The shapes here are the contract clients are written against, so they are declared once and
enforced on the way in as well as described on the way out. Declaring the phone number's
pattern on the request means a malformed number is refused in the same sentence a client
gets back, rather than becoming a support ticket later.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from apps.api.models.user import AccountStatus, PrimaryRole, VerificationTier
from packages.shared.i18n import Language
from packages.shared.phone import ETHIOPIAN_MOBILE_PATTERN

PHONE_PATTERN = ETHIOPIAN_MOBILE_PATTERN.pattern


class OtpRequestBody(BaseModel):
    """Ask for a verification code."""

    model_config = ConfigDict(extra="forbid")

    phone_e164: str = Field(
        pattern=PHONE_PATTERN,
        description="The trader's number in full international form.",
    )


class OtpRequestAccepted(BaseModel):
    """The code is on its way. Deliberately says nothing about whether an account exists."""

    model_config = ConfigDict(extra="forbid")

    expires_in_seconds: int = Field(description="How long the code stays usable.")
    resend_after_seconds: int = Field(
        description="How long before another code may be sent to this number."
    )


class OtpVerifyBody(BaseModel):
    """Prove ownership of the number and sign in."""

    model_config = ConfigDict(extra="forbid")

    phone_e164: str = Field(pattern=PHONE_PATTERN)
    code: str = Field(pattern=r"^\d{6}$", description="The code as received.")
    terms_version: str = Field(
        min_length=1,
        max_length=20,
        description="Version of the terms and privacy policy being accepted.",
    )
    language: Language | None = Field(
        default=None,
        description="Language to store for this trader. Defaults to Amharic.",
    )


class UserSummary(BaseModel):
    """The signed-in trader, as clients need to see them."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    phone_e164: str
    role: PrimaryRole
    language: Language
    kyc_tier: VerificationTier
    status: AccountStatus


class TokenPairResponse(BaseModel):
    """A signed-in session."""

    model_config = ConfigDict(extra="forbid")

    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    user: UserSummary


class RefreshTokenResponse(BaseModel):
    """A rotated session."""

    model_config = ConfigDict(extra="forbid")

    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
