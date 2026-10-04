"""Woreda repository."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.woreda import Woreda


class WoredaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self, woreda_id: int) -> bool:
        """Whether this woreda is one we hold.

        Checked against the table rather than against a list held in code, so that the
        answer follows the seed instead of the code that reads it.
        """
        stmt = select(Woreda.id).where(Woreda.id == woreda_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def list_by_region(self, region_code: str | None = None) -> Sequence[Woreda]:
        stmt = select(Woreda)
        if region_code:
            stmt = stmt.where(Woreda.region_code == region_code)
        stmt = stmt.order_by(Woreda.id)
        result = await self._session.execute(stmt)
        return result.scalars().all()
