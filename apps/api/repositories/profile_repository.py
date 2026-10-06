"""Profile repository.

One account has at most one profile, so every read here is by account rather than by
profile id: the caller is always whoever is signed in, never a caller-chosen row.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.profile import Profile


class ProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_user_id(self, user_id: uuid.UUID) -> Profile | None:
        """The one profile belonging to this account, if it has been filled in yet."""
        stmt = select(Profile).where(Profile.user_id == user_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def has_gps(self, user_id: uuid.UUID) -> bool:
        """Whether this trader has given a point that anything may be measured from.

        Asked of the database rather than read off a loaded profile: the coordinate is
        stored as geography and is deliberately not decoded on the way out, so a question
        about whether there is one is a question only the statement that can see the column
        can answer without pulling the point into Python first.
        """
        stmt = select(Profile.user_id).where(Profile.user_id == user_id, Profile.gps.is_not(None))
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def create(self, profile: Profile) -> Profile:
        self._session.add(profile)
        await self._session.flush()
        return profile

    async def save_changes(self, profile: Profile, changes: dict[str, object]) -> Profile:
        """Write only the fields the caller actually sent.

        The offline client merges field by field, so a request carrying one field must leave
        every other field as it was rather than resetting it to a default. The version goes
        up by one because the row did change, whichever fields those were.
        """
        for field, value in changes.items():
            setattr(profile, field, value)
        profile.version += 1
        await self._session.flush()
        return profile

    async def commit(self) -> None:
        """Make the merged row permanent.

        The version was bumped and the row flushed before this, so a commit that fails leaves
        nothing half-written.
        """
        await self._session.commit()
