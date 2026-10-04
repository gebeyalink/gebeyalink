"""Region repository.

Regions are reference data: thirteen rows, written by migration, read to check that a code
somebody sent is one the platform recognises.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.region import Region


class RegionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self, code: str) -> bool:
        """Whether this is one of the codes we seed.

        Checked against the table rather than against a list held in code, so that adding a
        region is a migration and not a release.
        """
        stmt = select(Region.code).where(Region.code == code)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None
