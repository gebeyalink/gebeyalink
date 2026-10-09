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

    async def find(self, code: str) -> Region | None:
        """One region by its code, for a screen that needs its name."""
        stmt = select(Region).where(Region.code == code)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self) -> list[Region]:
        """Every region the platform knows, in the order a menu should show them.

        Ordered by English name so that the menu stays stable across calls and across
        installs. Thirteen rows, seeded by migration and read whole.
        """
        stmt = select(Region).order_by(Region.name_en)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
