"""What a search asks for: which listings, in which order, and how many of them.

The specification puts every one of these in the query string rather than in a body, and
that is the right shape for it: a search is a link somebody can be sent, forwarded, or read
back over a voice call, and a link has to carry its own meaning.

Two of the fields are worth naming rather than leaving to be inferred from a type.

``sort`` is nullable here even though the specification gives it a default of
``distance_asc``. The default still applies - a search that says nothing about order sorts
by distance - but a cursor also carries the order that produced it, and a trader paging
through results should not have to restate that order on every request. ``None`` therefore
means "whatever this cursor already implies, or distance if there is no cursor", and an
order stated *and* contradicting the cursor is a client bug rather than a preference.

``radius_km`` is left as ``None`` rather than defaulted to 50 here, because the default only
means anything once it is known whether the trader has a point to measure from: FR-401 says
the radius requires the buyer's GPS, and a default applied to a trader who has none would
either refuse a search that was never about distance or silently measure nothing. Deciding
that is the service's business, not the query parser's.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import Literal

from apps.api.models.listing import DeliveryOption
from apps.api.models.user import VerificationTier

#: The thirteen regions of Ethiopia by their two-letter code, as the contract publishes them.
#: An unknown code is refused rather than treated as a region with nothing in it, because a
#: mistyped region returning an empty page looks exactly like a region where nothing is for
#: sale, and only one of those two is a bug.
RegionCode = Literal["AA", "AF", "AM", "BE", "DD", "GA", "HA", "OR", "SI", "SO", "SW", "SN", "TI"]


class SearchSort(enum.StrEnum):
    """FR-402's four orders, named exactly as ``docs/openapi.yaml`` publishes them."""

    DISTANCE_ASC = "distance_asc"
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"
    HARVEST_DATE_ASC = "harvest_date_asc"


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """One search as it arrived, before anything has decided what it means.

    This is deliberately plain data rather than a model: nothing here is validated or
    defaulted beyond what the query parser already refused, because the rules that turn
    these values into a page - which radius a trader is allowed, which order a cursor
    belongs to - are rules about the trader, not about the shape of a query string.
    """

    crop_id: int | None = None
    region_code: str | None = None
    woreda_id: int | None = None
    radius_km: int | None = None
    price_min: int | None = None
    price_max: int | None = None
    harvest_date_from: date | None = None
    harvest_date_to: date | None = None
    organic: bool | None = None
    delivery_option: DeliveryOption | None = None
    seller_tier_min: VerificationTier | None = None
    #: ``None`` means the caller did not say; see the module docstring for what that turns
    #: into once a cursor is known.
    sort: SearchSort | None = None
    cursor: str | None = None
    limit: int = 20
