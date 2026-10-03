"""The PostGIS geography type, declared once for both the schema and the models.

Boundaries are stored by PostGIS rather than as text, so that a point can later be tested
for membership of a woreda in the database instead of in Python. Nothing constructs a
geometry yet, so no geometry library is needed; all that is required is a name for the
column type, which is what this provides.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.types import UserDefinedType


class GeographyMultiPolygon(UserDefinedType[Any]):
    """`geography(MultiPolygon,4326)`: real-world metres, not degrees."""

    cache_ok = True

    def get_col_spec(self, **_: object) -> str:
        return "geography(MultiPolygon,4326)"
