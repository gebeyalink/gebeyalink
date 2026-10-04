"""Request and response bodies for reading and changing one's own profile.

The response is the contract's `Profile`: the account's own fields with the profile's beside
them. One field is deliberately absent from it.

There is no `gps`. The point a trader gives is kept for our own use and is not published, and
a schema is the one place where leaving a field off is the whole guarantee.

`additional_roles` is present and always empty. A trader holds exactly the one activity they
signed up for, so an empty list is the truthful answer today rather than a missing one, and
the response keeps the shape clients are written against. When a second activity becomes
possible, this list will start answering from where those are held.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from apps.api.models.user import AccountStatus, PrimaryRole, VerificationTier
from packages.shared.i18n.languages import Language


class GpsPoint(BaseModel):
    """A place, given as the trader would say it."""

    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class ProfileResponse(BaseModel):
    """The signed-in trader and whatever they have filled in about themselves."""

    model_config = ConfigDict(extra="forbid")

    # The account's own fields.
    id: UUID
    phone_e164: str
    role: PrimaryRole
    # Nothing else this trader does is recorded yet, so this is empty by construction rather
    # than by absence: one account carries one activity, and adding a second becomes
    # something this list will report.
    additional_roles: list[PrimaryRole]
    language: Language
    kyc_tier: VerificationTier
    status: AccountStatus
    terms_version: str | None

    # The profile's fields. Any of these may be null: a trader is not asked to describe
    # themselves before they have something to say.
    display_name: str | None
    region_code: str | None
    woreda_id: int | None
    kebele: str | None
    business_name: str | None
    license_no: str | None
    tax_identification_number: str | None


class ProfileUpdateBody(BaseModel):
    """A change to some of the caller's own fields.

    Every field is optional and every field that arrives is applied on its own: this is a
    merge, so an absent field means "leave it alone" rather than "clear it".

    `gps` is here because the platform accepts it, though the published contract does not yet
    list it; it is stored at reduced precision and never sent back.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=2, max_length=60)
    region_code: str | None = Field(default=None, min_length=2, max_length=2)
    woreda_id: int | None = None
    kebele: str | None = Field(default=None, max_length=80)
    language: Language | None = None
    business_name: str | None = None
    license_no: str | None = None
    tax_identification_number: str | None = None
    gps: GpsPoint | None = None
