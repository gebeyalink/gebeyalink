"""Reference endpoints.

Reference data is what the rest of the platform is written against: the regions a trader
lives in, the crops they grow, the woredas they trade from. It changes through reviewed
migrations rather than through the API, so these endpoints only ever read.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from apps.api.dependencies import AccessTokenDep, DbSessionDep
from apps.api.repositories.crop_repository import CropRepository
from apps.api.repositories.woreda_repository import WoredaRepository
from apps.api.schemas.reference import CropSummary, WoredaSummary

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
