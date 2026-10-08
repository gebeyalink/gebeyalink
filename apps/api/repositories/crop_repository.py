"""Crop repository."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.crop import Crop, CropBaseUnit


class CropRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_name(self, name: str) -> Crop | None:
        """The crop a sender named, matched against every language it is named in.

        The SMS grammar has one word for a crop and no identifier, so the word has to be
        matched against the catalogue. Matching is case-insensitive and trims surrounding
        space, because a handset adds neither consistently. An ambiguous name resolves to the
        lowest identifier, which is the catalogue's own order and so the same answer twice.
        """
        needle = name.strip().lower()
        stmt = (
            select(Crop)
            .where(
                or_(
                    func.lower(Crop.name_en) == needle,
                    func.lower(Crop.name_am) == needle,
                    func.lower(Crop.name_om) == needle,
                    func.lower(Crop.name_ti) == needle,
                )
            )
            .order_by(Crop.id)
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_by_id(self, crop_id: int) -> Crop | None:
        """One crop by the identifier the specification assigns it."""
        return await self._session.get(Crop, crop_id)

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
