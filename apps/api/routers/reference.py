"""Reference endpoints.

Reference data is what the rest of the platform is written against: the regions a trader
lives in, the crops they grow, the woredas they trade from, and the published price board
they compare an offer against. The first three change through reviewed migrations rather
than through the API and the board changes through the overnight aggregation, so every
endpoint here only ever reads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from apps.api.dependencies import AccessTokenDep, DbSessionDep
from apps.api.repositories.crop_repository import CropRepository
from apps.api.repositories.price_index_repository import PriceIndexRepository
from apps.api.repositories.woreda_repository import WoredaRepository
from apps.api.schemas.reference import CropSummary, PriceBoardResponse, WoredaSummary
from apps.api.schemas.search import RegionCode
from packages.shared.messaging.queues import EAT_ZONE

router = APIRouter(prefix="", tags=["Reference"])


@router.get("/crops", response_model=list[CropSummary])
async def list_crops(
    caller: AccessTokenDep,
    db: DbSessionDep,
) -> list[CropSummary]:
    """The crops a listing may be written against.

    Livestock and anything else measured by head rather than weight is left out: a price
    posted per kilogram would be a different product, not a cheaper one.
    """
    repo = CropRepository(db)
    crops = await repo.list_active_kg_crops()
    return [
        CropSummary(
            id=crop.id,
            name_en=crop.name_en,
            name_am=crop.name_am,
            name_om=crop.name_om,
            category=crop.category,
            base_unit=crop.base_unit,
            active=crop.active,
            escrow_hold_hours=crop.escrow_hold_hours,
        )
        for crop in crops
    ]


@router.get("/woredas", response_model=list[WoredaSummary])
async def list_woredas(
    caller: AccessTokenDep,
    db: DbSessionDep,
    region_code: str | None = Query(default=None),
) -> list[WoredaSummary]:
    """Woredas, narrowed to one region when the caller names one.

    Leaving the region out returns everything seeded so far, which is what a caller that
    has not narrowed yet wants.
    """
    repo = WoredaRepository(db)
    woredas = await repo.list_by_region(region_code)
    return [
        WoredaSummary(
            id=woreda.id,
            name=woreda.name,
            region_code=woreda.region_code,
            zone=woreda.zone,
        )
        for woreda in woredas
    ]


@router.get("/prices", response_model=PriceBoardResponse)
async def price_board(
    db: DbSessionDep,
    crop_id: Annotated[int, Query(ge=1, description="One crop, by the taxonomy's number.")],
    region_code: Annotated[RegionCode, Query(description="One of the thirteen regions.")],
) -> PriceBoardResponse:
    """FR-405's board, public: what this crop has been costing in this region, or that we
    do not know yet.

    Public on purpose, and that is a decision rather than an oversight: the board is the
    platform's answer to "is this price odd", and a buyer standing in a market with no
    account and a farmer checking whether today's offer is fair are exactly who needs to
    ask it.

    The latest published day is read rather than today's alone, because the aggregation runs
    at two in the morning and a board read at dawn is meant to be yesterday's arithmetic -
    that is the day it finished. No row at all is not a 404: the board exists and says there
    is not yet enough to say, which is the same honest answer a thin row gives, and a reader
    learns the same thing either way.
    """
    row = await PriceIndexRepository(db).latest_for(
        crop_id, region_code, datetime.now(EAT_ZONE).date()
    )
    if row is None:
        return PriceBoardResponse(
            avg_price_santim=None,
            min_price_santim=None,
            max_price_santim=None,
            sample_size=0,
            insufficient_data=True,
        )
    return PriceBoardResponse(
        avg_price_santim=row.avg_unit_price_santim,
        min_price_santim=row.min_unit_price_santim,
        max_price_santim=row.max_unit_price_santim,
        sample_size=row.sample_size,
        insufficient_data=row.insufficient_data,
    )
