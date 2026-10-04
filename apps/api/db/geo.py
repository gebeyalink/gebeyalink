"""The PostGIS geography type, declared once for both the schema and the models.

Boundaries and points are stored by PostGIS rather than as text, so that a point can be
tested for membership of a woreda in the database instead of in Python. Nothing constructs
a geometry in Python yet, so no geometry library is needed; all that is required is a name
for the column type, which is what this provides.

`geography` rather than `geometry` throughout: it measures in metres, so a distance or an
area does not change meaning with latitude.
"""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import BindParameter, ColumnElement, func
from sqlalchemy.types import TypeDecorator, UserDefinedType


class _Geography(UserDefinedType[Any]):
    """A `geography` column in the WGS84 reference system."""

    cache_ok = True

    geometry_type: ClassVar[str]

    def get_col_spec(self, **_: object) -> str:
        return f"geography({self.geometry_type},4326)"


class GeographyPoint(_Geography):
    """`geography(Point,4326)`: one place, such as where a trader says they are."""

    geometry_type = "Point"


class GeographyMultiPolygon(_Geography):
    """`geography(MultiPolygon,4326)`: an area, such as a woreda boundary."""

    geometry_type = "MultiPolygon"


class LatitudeLongitude(TypeDecorator[tuple[float, float]]):
    """A coordinate pair, stored as `geography(Point,4326)`.

    The pair is given latitude first, because that is the order people say it in, and PostGIS
    takes it longitude first, because that is the order the coordinate system is defined in.
    The swap happens here so that no caller has to remember it.

    The value is handed to the driver as text and turned into a point by PostGIS inside the
    statement that writes it, because a coordinate pair is not something a driver can send on
    its own. The text is the well-known EWKT form, which carries the reference system with it
    so the column cannot end up holding an unlabelled point.

    Reading one of these back gives the raw stored value, which is deliberate: the platform
    stores a point for its own use and does not hand it out, so nothing needs to decode it.
    """

    impl = GeographyPoint
    cache_ok = True

    def process_bind_param(self, value: tuple[float, float] | None, dialect: Any) -> str | None:
        """Render the pair as EWKT: `SRID=4326;POINT(longitude latitude)`."""
        if value is None:
            return None
        latitude, longitude = value
        return f"SRID=4326;POINT({longitude} {latitude})"

    def bind_expression(self, bindvalue: BindParameter[Any]) -> ColumnElement[Any]:
        """Have PostGIS read that text, so the driver never has to send a geometry."""
        return func.ST_GeogFromText(bindvalue)
