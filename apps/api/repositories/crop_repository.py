"""Crop repository."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.crop import Crop, CropBaseUnit


class CropRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active_kg_crops(self) -> Sequence[Crop]:
        """The crops that can be priced by the kilogram, in taxonomy order.

        Ordering by the identifier rather than the name keeps the response stable as
        languages are added, and matches the order the catalogue is published in.
        """
        stmt = (
            select(Crop)
            .where(Crop.active.is_(True), Crop.base_unit == CropBaseUnit.KG)
            .order_by(Crop.id)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()
