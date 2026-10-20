from __future__ import annotations

from pydantic import BaseModel

from apps.api.models.crop import CropBaseUnit, CropCategory


class CropSummary(BaseModel):
    """A crop as the contract describes it.

    Tigrinya is carried in the database for a future pilot but is not part of this contract,
    so it is not sent; `name_om` is, because the contract asks for it.
    """

    id: int
    name_en: str
    name_am: str
    name_om: str | None
    category: CropCategory
    base_unit: CropBaseUnit
    active: bool
    escrow_hold_hours: int


class WoredaSummary(BaseModel):
    id: int
    name: str
    region_code: str
    zone: str


class PriceBoardResponse(BaseModel):
    """FR-405's board for one crop in one region: the published day's five figures.

    The three prices are withheld together when the day judged too little to say, which is
    what `insufficient_data` carries; an average with no edges beside it, or edges with no
    average, would be the very presentation the thin-sample rule exists to refuse.
    """

    avg_price_santim: int | None
    min_price_santim: int | None
    max_price_santim: int | None
    sample_size: int
    insufficient_data: bool
