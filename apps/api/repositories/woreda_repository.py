"""Woreda repository."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.woreda import Woreda


class WoredaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_region(self, region_code: str | None = None) -> Sequence[Woreda]:
        stmt = select(Woreda)
        if region_code:
            stmt = stmt.where(Woreda.region_code == region_code)
        stmt = stmt.order_by(Woreda.id)
        result = await self._session.execute(stmt)
        return result.scalars().all()
