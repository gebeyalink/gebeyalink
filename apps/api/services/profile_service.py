"""The rules for reading and changing a trader's own profile.

A profile is the one entity a client owns outright: it is edited by the person it describes
and nobody else, and the offline client merges it a field at a time. Two consequences shape
this service.

First, a missing profile row is not an error. Somebody who has only ever signed in has
nothing to say about themselves yet, and asking them to describe themselves is not our
business. So reading is answered from whatever exists, and the row appears on the first
write rather than on the first read.

Second, an update is a merge, not a replacement. A request carrying one field leaves the
others exactly as they were, because the client that sent it may hold a different copy of
the rest and has no way to find out what we hold.

The GPS point is accepted, rounded to five decimal places, and then not spoken about again.
It is in no response and in no error.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from apps.api.models.profile import Profile
from apps.api.models.user import User
from apps.api.repositories.profile_repository import ProfileRepository
from apps.api.repositories.region_repository import RegionRepository
from apps.api.repositories.user_repository import UserRepository
from apps.api.repositories.woreda_repository import WoredaRepository
from packages.shared.errors import ApiError, ErrorCode
from packages.shared.i18n.languages import Language

#: How many decimal places a coordinate is kept at. Five places is about a metre: finer than
#: a phone can honestly place somebody, coarser than the noise in a fix.
GPS_DECIMAL_PLACES = 5


@dataclass(frozen=True, slots=True)
class ProfileChanges:
    """The fields a caller sent, each absent unless it really was sent.

    Optionals rather than a dictionary so that "not mentioned" cannot be confused with
    "sent as nothing", and so that the service never has to guess what a missing key meant.
    """

    display_name: str | None = None
    region_code: str | None = None
    woreda_id: int | None = None
    kebele: str | None = None
    gps: tuple[float, float] | None = None
    language: Language | None = None
    business_name: str | None = None
    license_no: str | None = None
    tax_identification_number: str | None = None


class ProfileService:
    def __init__(
        self,
        profiles: ProfileRepository,
        users: UserRepository,
        regions: RegionRepository,
        woredas: WoredaRepository,
    ) -> None:
        self._profiles = profiles
        self._users = users
        self._regions = regions
        self._woredas = woredas

    async def read(self, user_id: uuid.UUID) -> tuple[User, Profile | None]:
        """The account, and the profile beside it if one has been filled in yet."""
        user = await self._require_user(user_id)
        return user, await self._profiles.find_by_user_id(user_id)

    async def update(
        self, user_id: uuid.UUID, changes: ProfileChanges
    ) -> tuple[User, Profile | None]:
        """Merge the fields that were sent into this trader's profile, creating it if new."""
        user = await self._require_user(user_id)
        profile_values = await self._checked_profile_values(changes)

        if changes.language is not None:
            user = await self._users.save_language(user, changes.language)

        existing = await self._profiles.find_by_user_id(user_id)
        if existing is None:
            if not profile_values:
                # Nothing about the profile to write. A language on its own is still a change
                # and still has to be committed before the answer is built from it.
                if changes.language is not None:
                    await self._commit()
                return user, None
            profile = await self._profiles.create(Profile(user_id=user_id, **profile_values))
            await self._commit()
            return user, profile

        if not profile_values:
            # The row already exists and there is nothing in this request to put on it. The
            # stored profile is untouched, and so is its version: nothing has overtaken a
            # client holding a copy of it.
            if changes.language is not None:
                await self._commit()
            return user, existing

        profile = await self._profiles.save_changes(existing, profile_values)
        await self._commit()
        return user, profile

    async def _commit(self) -> None:
        """Make the write real.

        Committed here rather than left to the request so that a profile has outlived the
        response that confirmed it by the time the next request reads it. A failure partway
        through leaves the stored profile untouched, because the checks above all run before
        anything is written.
        """
        await self._profiles.commit()

    async def _require_user(self, user_id: uuid.UUID) -> User:
        user = await self._users.find_by_id(user_id)
        if user is None:  # pragma: no cover - a valid token names an account that signed in
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "user"})
        return user

    async def _checked_profile_values(self, changes: ProfileChanges) -> dict[str, Any]:
        """Check what arrived against what exists, and hand back only the profile's own fields.

        Every lookup runs before anything is written, so a request naming a region that does
        not exist leaves the stored profile exactly as it was rather than half-updated.
        Language is not here: it belongs to the account, not the profile.
        """
        problems: dict[str, str] = {}

        region_code = changes.region_code
        if region_code is not None and not await self._regions.exists(region_code):
            problems["region_code"] = "not_a_seeded_region"

        woreda_id = changes.woreda_id
        if woreda_id is not None and not await self._woredas.exists(woreda_id):
            # A woreda that does not exist is a reference to nothing, which reads as a missing
            # resource rather than a malformed field: the caller may have a stale identifier
            # and needs to be told which one is gone.
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "woreda", "id": woreda_id})

        display_name = changes.display_name
        if display_name is not None and not _display_name_is_acceptable(display_name):
            problems["display_name"] = "must_be_2_to_60_characters"

        if problems:
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": problems})

        values: dict[str, Any] = {}
        if display_name is not None:
            values["display_name"] = display_name
        if region_code is not None:
            values["region_code"] = region_code
        if woreda_id is not None:
            values["woreda_id"] = woreda_id
        if changes.kebele is not None:
            values["kebele"] = changes.kebele
        if changes.gps is not None:
            values["gps"] = _round_coordinates(changes.gps)
        if changes.business_name is not None:
            values["business_name"] = changes.business_name
        if changes.license_no is not None:
            values["license_no"] = changes.license_no
        if changes.tax_identification_number is not None:
            values["tax_identification_number"] = changes.tax_identification_number
        return values


def _display_name_is_acceptable(value: str) -> bool:
    """Two to sixty characters, with nothing padded at either end.

    A name that arrived as only spaces satisfies the length rule and is not a name, so the
    trimmed form has to be the form that was sent.
    """
    return 2 <= len(value) <= 60 and value.strip() == value and value.strip() != ""


def _round_coordinates(point: tuple[float, float]) -> tuple[float, float]:
    """Round a coordinate pair to the precision the platform keeps.

    Rounded on the way in rather than on the way out: the stored value is the value the
    trader gave, to the precision it is worth, and there is nothing to expose later anyway.
    """
    latitude, longitude = point
    return (round(latitude, GPS_DECIMAL_PLACES), round(longitude, GPS_DECIMAL_PLACES))
