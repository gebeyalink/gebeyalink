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
