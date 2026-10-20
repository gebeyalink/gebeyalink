"""Price index repository.

One read, and it asks a narrow question: what did this crop last cost in this region, as at
this day. Nothing here recomputes anything, because a check that recalculates a price is no
longer a check against what the platform published.

And one write, which belongs to the overnight aggregation alone: the day's board, written
over itself if it is written twice. The two halves are together because they are the same
statement made on two schedules - the board published in the morning, the board read when
somebody asks - and neither is any use without the other.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.price_index import PriceIndex


@dataclass(frozen=True, slots=True)
class DailyIndex:
    """One (crop, region) pair's board for one day, as the overnight job computed it.

    All three prices are None or all three are figures, never one of each: a minimum with
    no average beside it would be the exact presentation the thin-sample rule refuses.
    """

    crop_id: int
    region_code: str
    day: date
    avg_unit_price_santim: int | None
    min_unit_price_santim: int | None
    max_unit_price_santim: int | None
    sample_size: int
    insufficient_data: bool


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

    async def publish(self, rows: Sequence[DailyIndex]) -> None:
        """Write a day's board, once per pair however many times the job runs.

        An upsert against the table's own unique key, so the run that succeeds twice leaves
        the same row the run that succeeded once would have: a second write replaces the
        first with the same figures, because both read the same window. That is also why
        this task needs no idempotency store of its own - the database already knows how to
        be asked the same question twice.

        A pair the window never saw is not written at all. Its answer is the absence of a
        row, which the readers already treat as "nothing to quote", and a row saying so for
        every crop in the country would be a table of nothings.
        """
        if not rows:
            return
        values = [
            {
                "crop_id": row.crop_id,
                "region_code": row.region_code,
                "day": row.day,
                "avg_unit_price_santim": row.avg_unit_price_santim,
                "min_unit_price_santim": row.min_unit_price_santim,
                "max_unit_price_santim": row.max_unit_price_santim,
                "sample_size": row.sample_size,
                "insufficient_data": row.insufficient_data,
            }
            for row in rows
        ]
        statement = insert(PriceIndex).values(values)
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["crop_id", "region_code", "day"],
                set_={
                    "avg_unit_price_santim": statement.excluded.avg_unit_price_santim,
                    "min_unit_price_santim": statement.excluded.min_unit_price_santim,
                    "max_unit_price_santim": statement.excluded.max_unit_price_santim,
                    "sample_size": statement.excluded.sample_size,
                    "insufficient_data": statement.excluded.insufficient_data,
                    "updated_at": func.now(),
                },
            )
        )

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
