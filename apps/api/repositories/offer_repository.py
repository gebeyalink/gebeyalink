"""Offer repository.

An offer is never read without the listing it is on, because almost every rule about an offer is
a rule about the listing underneath it: the price ceiling, the minimum weight, whether the
listing is live, and who is allowed to answer. The listing is therefore fetched alongside the
offer rather than left to be loaded on attribute access — a relationship that loads itself would
try to reach the database from a plain attribute read, which in an async request cannot be
awaited and would fail only under load.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.listing import Listing
from apps.api.models.offer import Offer, OfferStatus


@dataclass(frozen=True, slots=True)
class OfferInContext:
    """An offer together with the listing whose terms bound it."""

    offer: Offer
    listing: Listing


class OfferRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, offer: Offer) -> Offer:
        """Add an offer and give it its identity, without making the write permanent."""
        self._session.add(offer)
        await self._session.flush()
        return offer

    async def find_in_context(self, offer_id: uuid.UUID) -> OfferInContext | None:
        """One offer by its internal identity, with the listing it belongs to."""
        stmt = (
            select(Offer, Listing)
            .join(Listing, Listing.id == Offer.listing_id)
            .where(Offer.id == offer_id)
        )
        result = await self._session.execute(stmt)
        row = result.one_or_none()
        if row is None:
            return None
        return OfferInContext(offer=row[0], listing=row[1])

    async def list_for_listing(self, listing_id: uuid.UUID) -> list[Offer]:
        """Every offer on a listing, oldest first.

        Oldest first because a negotiation is read in the order it happened: the seller opening
        this list is following a thread, and a thread whose first bid is below its third answer
        is not a thread anybody can follow.
        """
        stmt = (
            select(Offer).where(Offer.listing_id == listing_id).order_by(Offer.created_at, Offer.id)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_still_open(self, listing_id: uuid.UUID, now: datetime) -> int:
        """How many offers on this listing are open and have not lapsed.

        Counted against the clock rather than read off the stored status, because expiry is
        derived and not written (§FR-503 lazy expiry). Counting the stored `OPEN` instead would
        hold a lapsed offer against the cap for as long as the row survives, and a listing whose
        buyers had all walked away would refuse new bids for no reason a caller could see.
        """
        stmt = (
            select(func.count())
            .select_from(Offer)
            .where(
                Offer.listing_id == listing_id,
                Offer.status == OfferStatus.OPEN,
                Offer.valid_until > now,
            )
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def commit(self) -> None:
        await self._session.commit()

    async def flush(self) -> None:
        await self._session.flush()
