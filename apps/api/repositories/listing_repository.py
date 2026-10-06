"""Listing repository.

A listing is always read with its photographs, and never apart from them, so the two are
carried together as one value rather than as a relationship that loads itself. That is not
stylistic: a relationship left to load on attribute access would try to reach the database
from a plain attribute read, which in an async request cannot be awaited and would fail only
under load.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.listing import Listing, ListingPhoto


@dataclass(frozen=True, slots=True)
class LoadedListing:
    """A listing and the photographs that belong to it."""

    listing: Listing
    photos: list[ListingPhoto]


class ListingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, listing: Listing) -> Listing:
        """Add a listing and give it its identity, without making the write permanent."""
        self._session.add(listing)
        await self._session.flush()
        return listing

    async def find_by_id(self, listing_id: uuid.UUID) -> LoadedListing | None:
        """One listing by its internal identity, photographs included."""
        stmt = select(Listing).where(Listing.id == listing_id)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        photographs = await self._photos_for([row.id])
        return LoadedListing(listing=row, photos=photographs.get(row.id, []))

    async def find_by_id_for_update(self, listing_id: uuid.UUID) -> LoadedListing | None:
        """One listing by its internal identity, with the row locked until this transaction ends.

        Locked *before* availability is computed rather than after. Everything that follows -
        what is left, whether a competing offer still fits, whether this sale empties the listing
        - is derived from `quantity_kg`, so two sales could otherwise both read the same
        remaining kilos and both believe they had them. The second one waits here and reads what
        the first one left.

        `populate_existing` because the row may already be in this session's identity map from an
        earlier read in the same request. Without it the locked read would return the copy held
        in memory, which is the value read *before* the lock was taken, and the lock would be
        held over a stale quantity - the one thing the lock is for.

        Not using `find_by_id` because that one is also the entry point to reading a listing for
        display, where a lock would be held by a request that had no reason to hold one.
        """
        stmt = (
            select(Listing)
            .where(Listing.id == listing_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        photographs = await self._photos_for([row.id])
        return LoadedListing(listing=row, photos=photographs.get(row.id, []))

    async def page_for_seller(
        self,
        seller_id: uuid.UUID,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> tuple[list[LoadedListing], bool]:
        """One page of a seller's own listings, newest first, and whether more remain.

        The page is cut at ``limit + 1`` rows so that "is there another" is answered by the
        rows themselves rather than by a second count query. The extra row is not returned:
        it exists only to be looked at.
        """
        stmt = select(Listing).where(Listing.seller_id == seller_id)
        if after is not None:
            after_created_at, after_id = after
            stmt = stmt.where(tuple_(Listing.created_at, Listing.id) < (after_created_at, after_id))
        stmt = stmt.order_by(Listing.created_at.desc(), Listing.id.desc()).limit(limit + 1)
        result = await self._session.execute(stmt)
        rows = list(result.scalars().all())

        photographs = await self._photos_for([row.id for row in rows[:limit]])
        return [
            LoadedListing(listing=row, photos=photographs.get(row.id, [])) for row in rows[:limit]
        ], len(rows) > limit

    async def replace_photos(
        self, listing_id: uuid.UUID, photos: list[tuple[str, int, int]]
    ) -> list[ListingPhoto]:
        """Set a listing's photographs to exactly the set given, in the order given.

        Replaced wholesale rather than diffed. A seller's intent is "these are my photos,
        this is the order", and reconciling that against what is stored would need a rule for
        which of two intentions a particular request expressed.
        """
        await self._delete_photos_of(listing_id)
        # The removals have to reach the database before the replacements do. A flush orders
        # inserts ahead of deletes, so without this the first new photograph would be written
        # over the sort order of the old one still sitting there, and the unique constraint on
        # (listing, sort order) would refuse a perfectly good set of pictures.
        await self._session.flush()
        added = [
            ListingPhoto(
                listing_id=listing_id,
                url=url,
                width=width,
                height=height,
                sort_order=position,
            )
            for position, (url, width, height) in enumerate(photos)
        ]
        self._session.add_all(added)
        await self._session.flush()
        return added

    async def short_code_exists(self, short_code: str) -> bool:
        """Whether this code is already spoken for.

        Asked before writing rather than caught afterwards, so a collision is an ordinary
        retry rather than a failed transaction the caller has to see.
        """
        stmt = select(func.count()).select_from(Listing).where(Listing.short_code == short_code)
        result = await self._session.execute(stmt)
        count = result.scalar_one()
        return bool(count)

    async def commit(self) -> None:
        await self._session.commit()

    async def flush(self) -> None:
        await self._session.flush()

    async def _photos_for(
        self, listing_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[ListingPhoto]]:
        """Every photograph belonging to these listings, grouped and in stored order."""
        if not listing_ids:
            return {}
        stmt = (
            select(ListingPhoto)
            .where(ListingPhoto.listing_id.in_(listing_ids))
            .order_by(ListingPhoto.listing_id, ListingPhoto.sort_order)
        )
        result = await self._session.execute(stmt)
        grouped: dict[uuid.UUID, list[ListingPhoto]] = {}
        for photo in result.scalars().all():
            grouped.setdefault(photo.listing_id, []).append(photo)
        return grouped

    async def _delete_photos_of(self, listing_id: uuid.UUID) -> None:
        stmt = select(ListingPhoto).where(ListingPhoto.listing_id == listing_id)
        result = await self._session.execute(stmt)
        for existing in result.scalars().all():
            await self._session.delete(existing)
