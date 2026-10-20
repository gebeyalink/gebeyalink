"""The daily regional price board: what a crop has been costing, over the trailing month.

The board exists so a price can be judged rather than merely seen. A single listing at
twice yesterday's figure means nothing on its own — a farmer may be asking what they like —
and the question every reader of the board is really asking is "is this number odd?" The
answer has to come from the window, not from the request, and it has to be the same answer
every time, so the arithmetic lives here and the SMS price command, the USSD price path and
the public endpoint all read what this wrote rather than computing anything of their own.

Two decisions are stated rather than left to be inferred.

**The index is a weighted median, in integers throughout.** A mean would be dragged about
by one desperate sale at a third of the going rate, which is exactly the figure a reader
needs protection from. So each observation carries weight — what a funded sale is worth
against what an asking price is worth, scaled by kilos — and the index is the first observed
price whose accumulated weight reaches half of all of it. No floats are involved anywhere,
and no division either: half the weight is found by doubling what has accumulated, so the
answer is always a price somebody actually asked for or paid, never a figure standing
between two of them.

**The window is thirty EAT calendar days, ending before today.** The board answers "what has
this been costing recently", and a window that included the day still unfolding would answer
a different question every time it was asked. The edges are Addis's days, not UTC's: a trade
placed a minute before midnight in Addis belongs to that day however the server's clock
would file it.

What is persisted alongside the index is the same statement three ways: the index, the two
edges it sits between, and how many observations the three rest on. When the sample is thin
all three figures are withheld together, because a range over two points is not a range.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Final

from apps.api.repositories.listing_repository import ListingRepository
from apps.api.repositories.order_repository import OrderRepository
from apps.api.repositories.price_index_repository import DailyIndex, PriceIndexRepository
from packages.shared.messaging.queues import EAT_ZONE

#: How far back the board looks: the thirty EAT calendar days before the day it is written
#: for, ending yesterday. A constant rather than a configuration, because a deployment that
#: could quietly shorten the window would be publishing a different board under the same
#: name to the same readers.
TRADING_WINDOW_DAYS: Final[int] = 30

#: How thin the evidence may get before the board declines to state a price. Two points can
#: only ever say "somewhere between these two", and a range of two is precisely the
#: misleading presentation this method exists to prevent; three is the least count at which
#: a median sits between observations instead of on top of one.
MINIMUM_SAMPLE_SIZE: Final[int] = 3

#: What one funded sale is worth against one asking price, before kilos are counted: three
#: times them. A listing is what somebody hopes to get; an order with the platform's money
#: behind it is what somebody actually agreed. The method weights the fact accordingly.
FUNDED_ORDER_WEIGHT: Final[int] = 3


def volume_weighted_median(observations: Iterable[tuple[int, int]]) -> int:
    """The unit price at which half the window's weight sits, by value.

    Takes ``(unit price in santim, weight)`` pairs and returns the first observed price
    whose accumulated weight, doubled, reaches the total. Integer arithmetic only, and no
    division: "half" is found by doubling what has accumulated, so the answer is always one
    of the prices that was actually asked or paid for — never a figure between two of them,
    and never a float's rounding of one.

    The sort is by price, and equal prices cost nothing either way: whichever of two equal
    observations reaches the halfway line first, the line is at that price.
    """
    ordered = sorted(observations)
    total_weight = sum(weight for _, weight in ordered)
    accumulated = 0
    for unit_price_santim, weight in ordered:
        accumulated += weight
        if 2 * accumulated >= total_weight:
            return unit_price_santim
    raise ValueError("a median needs at least one observation")


class PricingService:
    """The overnight aggregation: the board, computed from the trades behind it.

    Nothing here runs on a request path. The table it writes is read by the SMS price
    command, the USSD price path and the public endpoint; the arithmetic lives in one place
    so all three read the same judgement and none of them makes one.
    """

    def __init__(
        self,
        *,
        orders: OrderRepository,
        listings: ListingRepository,
        price_indices: PriceIndexRepository,
    ) -> None:
        self._orders = orders
        self._listings = listings
        self._price_indices = price_indices

    async def compute_and_persist(self, *, now: datetime | None = None) -> int:
        """Compute the board for every pair the trailing window saw, and publish the day.

        Returns the number of (crop, region) pairs written. Pairs the window never saw are
        not written at all: their answer is the absence of a row, which the readers already
        treat as "nothing to quote", and a row saying so for every crop in the country would
        be a table of nothings.

        `now` is injectable because the window's edges are the whole point of the test: a
        boundary only means what it means if the day it names can be put under the clock
        without waiting a month for the calendar.
        """
        moment = now if now is not None else datetime.now(UTC)
        day = moment.astimezone(EAT_ZONE).date()
        first_day = day - timedelta(days=TRADING_WINDOW_DAYS)
        last_day = day - timedelta(days=1)

        by_pair: dict[tuple[int, str], list[tuple[int, int]]] = defaultdict(list)
        for (
            crop_id,
            region_code,
            unit_price_santim,
            quantity_kg,
        ) in await self._orders.funded_price_observations(first_day, last_day):
            by_pair[(crop_id, region_code)].append(
                (unit_price_santim, FUNDED_ORDER_WEIGHT * quantity_kg)
            )
        for (
            crop_id,
            region_code,
            unit_price_santim,
            quantity_kg,
        ) in await self._listings.priced_observations(first_day, last_day):
            by_pair[(crop_id, region_code)].append((unit_price_santim, quantity_kg))

        published = [
            _daily_index(day, crop_id, region_code, observations)
            for (crop_id, region_code), observations in sorted(by_pair.items())
        ]
        await self._price_indices.publish(published)
        await self._price_indices.commit()
        return len(published)


def _daily_index(
    day: date,
    crop_id: int,
    region_code: str,
    observations: list[tuple[int, int]],
) -> DailyIndex:
    """One pair's row: the index, the two edges, and how thin the evidence was.

    Below the minimum, all three prices are withheld together. A minimum of two points is
    not a minimum — it is the two prices somebody happened to ask, dressed as a range — and
    refusing to publish is the whole reason a reader can trust the figures when they do
    appear.
    """
    sample_size = len(observations)
    if sample_size < MINIMUM_SAMPLE_SIZE:
        return DailyIndex(
            crop_id=crop_id,
            region_code=region_code,
            day=day,
            avg_unit_price_santim=None,
            min_unit_price_santim=None,
            max_unit_price_santim=None,
            sample_size=sample_size,
            insufficient_data=True,
        )
    prices = [unit_price_santim for unit_price_santim, _ in observations]
    return DailyIndex(
        crop_id=crop_id,
        region_code=region_code,
        day=day,
        avg_unit_price_santim=volume_weighted_median(observations),
        min_unit_price_santim=min(prices),
        max_unit_price_santim=max(prices),
        sample_size=sample_size,
        insufficient_data=False,
    )
