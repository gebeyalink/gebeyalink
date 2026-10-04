"""Price index repository.

One read, and it asks a narrow question: what did this crop last cost in this region, as at
this day. Nothing here recomputes anything, because a check that recalculates a price is no
longer a check against what the platform published.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.price_index import PriceIndex


class PriceIndexRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_for(self, crop_id: int, region_code: str, as_of: date) -> PriceIndex | None:
        """The most recent published row for this crop and region, up to and including a day.

        Reading the latest rather than only today's row is deliberate. The aggregation may not
        have run for today yet, and a price check that silently declined to judge because
        this morning's job was late would be a different rule from the one that says it judges
        a price when it can. The row that exists is the row that was published; whether it
        has judged enough data is answered separately, from the row itself.
        """
        stmt = (
            select(PriceIndex)
            .where(
                PriceIndex.crop_id == crop_id,
                PriceIndex.region_code == region_code,
                PriceIndex.day <= as_of,
            )
            .order_by(PriceIndex.day.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
