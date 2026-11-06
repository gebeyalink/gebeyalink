"""Reading and writing saved searches, and finding what is new for each of them.

The repository is the only place that speaks SQL for a saved search. Two of its queries are
worth naming rather than leaving to be read out of the statements.

The first is the ten-per-user limit. Counting before an insert is only a limit if nothing
else can insert between the count and the write, so the service locks the account row and
this repository counts under that lock. Two requests from the same handset then take turns
rather than both seeing nine.

The second is the digest. ``list_saved_searches_with_new_matches`` takes a map from saved
search to the moment its window opens, so the caller decides what "new" means for each row
and this method applies the same filters the search endpoint applies. A window that is
null in the table is resolved by the caller to the search's own creation time; this method
never widens a window of its own. The result carries the full count per search and only the
first ``limit_per_search`` listing identities, because a digest names a search rather than
reproducing the market and a search that matches ten thousand listings should not make the
worker hold ten thousand.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from apps.api.models.crop import Crop
from apps.api.models.listing import Listing, ListingStatus
from apps.api.models.profile import Profile
from apps.api.models.region import Region
from apps.api.models.saved_search import SavedSearch
from apps.api.models.user import AccountStatus, User, VerificationTier
from apps.api.models.woreda import Woreda


@dataclass(frozen=True, slots=True)
class SavedSearchDigest:
    """One saved search that has new listings, with what the worker needs to name it.

    ``new_listing_count`` is the whole count inside the window, not the length of
    ``new_listing_ids``: the latter is capped at the caller's ``limit_per_search`` so the
    query has a bound, while the former is what the line "N new listings" says.
    """

    search: SavedSearch
    user: User
    crop: Crop | None
    region: Region | None
    new_listing_count: int
    new_listing_ids: tuple[UUID, ...]


_MINIMUM_TIER_RANK = case(
    (SavedSearch.seller_tier_min == VerificationTier.TIER_0, 0),
    (SavedSearch.seller_tier_min == VerificationTier.TIER_1, 1),
    (SavedSearch.seller_tier_min == VerificationTier.TIER_2, 2),
    else_=0,
)


class SavedSearchRepository:
    """Saved-search reads and writes, over one request's or one run's session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user(self, user_id: UUID) -> list[SavedSearch]:
        """The caller's searches, newest first."""
        result = await self._session.execute(
            select(SavedSearch)
            .where(SavedSearch.user_id == user_id)
            .order_by(SavedSearch.created_at.desc(), SavedSearch.id.desc())
        )
        return list(result.scalars().all())

    async def lock_user(self, user_id: UUID) -> None:
        """Serialize this account's saved-search writes for the rest of the transaction.

        The row locked is the account, not the search table: there is no row to lock for a
        search that does not exist yet, and locking everything the account owns would make
        two different accounts' writes contend for no reason.
        """
        await self._session.execute(select(User.id).where(User.id == user_id).with_for_update())

    async def count_for_user(self, user_id: UUID) -> int:
        """How many searches this account already holds."""
        result = await self._session.execute(
            select(func.count()).select_from(SavedSearch).where(SavedSearch.user_id == user_id)
        )
        return int(result.scalar_one())

    async def add(self, search: SavedSearch) -> SavedSearch:
        """Stage one search and flush it so its identity is known before the commit."""
        self._session.add(search)
        await self._session.flush()
        return search

    async def delete_for_user(self, user_id: UUID, search_id: UUID) -> int:
        """Delete one of this account's searches and say how many rows went.

        A search belonging to somebody else is not visible to the statement, so the answer
        is zero rather than a refusal that would confirm the row exists.
        """
        result = await self._session.execute(
            delete(SavedSearch).where(
                SavedSearch.id == search_id,
                SavedSearch.user_id == user_id,
            )
        )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def list_digest_since_map(self) -> dict[UUID, datetime]:
        """Every digest-enabled search on an active account, and its window's start.

        A search with no previous digest is measured from its own ``created_at``. That is
        the same rule the worker states in words; keeping it in this one query means a
        caller cannot accidentally pass ``None`` into the CASE below and widen the window
        to the beginning of the market.
        """
        result = await self._session.execute(
            select(
                SavedSearch.id,
                SavedSearch.created_at,
                SavedSearch.last_digest_at,
            )
            .join(User, User.id == SavedSearch.user_id)
            .where(
                SavedSearch.digest.is_(True),
                User.status == AccountStatus.ACTIVE,
            )
        )
        return {
            search_id: last_digest_at or created_at
            for search_id, created_at, last_digest_at in result.all()
        }

    async def list_saved_searches_with_new_matches(
        self, since_map: dict[UUID, datetime], limit_per_search: int = 5
    ) -> list[SavedSearchDigest]:
        """The searches in ``since_map`` that have at least one new matching listing.

        The SQL builds one row per (search, matching listing), ranks each listing inside
        its search by age, and then keeps only the first ``limit_per_search``. The count
        is a window over the unranked rows, so it remains the true count after the cap;
        grouping the capped rows gives one digest entry per search.

        The filters are the search endpoint's filter set as conditions between the saved
        search and the listing. A radius is applied only when the owner has a point to
        measure from, the same rule the search service states for a one-off search. A search
        whose owner has since cleared their point therefore keeps matching on its other
        filters rather than silently matching nothing.
        """
        if not since_map:
            return []

        limit = max(1, limit_per_search)
        now = datetime.now(UTC)
        owner = aliased(User, name="saved_search_owner")
        seller = aliased(User, name="saved_search_seller")
        seller_tier_rank = case(
            (seller.kyc_tier == VerificationTier.TIER_0, 0),
            (seller.kyc_tier == VerificationTier.TIER_1, 1),
            (seller.kyc_tier == VerificationTier.TIER_2, 2),
            else_=0,
        )

        # Each search's own window. The caller resolved nulls to created_at, but the
        # else-branch is the same answer rather than a wider one if a caller ever passes a
        # deliberately sparse map.
        since = case(
            *[(SavedSearch.id == search_id, moment) for search_id, moment in since_map.items()],
            else_=SavedSearch.created_at,
        )

        owner_has_point = (
            select(Profile.user_id)
            .where(Profile.user_id == SavedSearch.user_id, Profile.gps.is_not(None))
            .exists()
        )
        distance = func.ST_Distance(
            func.ST_Centroid(Woreda.geog),
            select(Profile.gps).where(Profile.user_id == SavedSearch.user_id).scalar_subquery(),
        )

        matching = select(
            SavedSearch.id.label("search_id"),
            Listing.id.label("listing_id"),
            func.count().over(partition_by=SavedSearch.id).label("new_listing_count"),
            func.row_number()
            .over(
                partition_by=SavedSearch.id,
                order_by=(Listing.created_at.desc(), Listing.id.desc()),
            )
            .label("position"),
        )
        matching = (
            matching.join(
                Listing,
                and_(
                    or_(SavedSearch.crop_id.is_(None), Listing.crop_id == SavedSearch.crop_id),
                    or_(
                        SavedSearch.region_code.is_(None),
                        Listing.region_code == SavedSearch.region_code,
                    ),
                    or_(
                        SavedSearch.woreda_id.is_(None),
                        Listing.woreda_id == SavedSearch.woreda_id,
                    ),
                    or_(
                        SavedSearch.price_min.is_(None),
                        Listing.unit_price_santim >= SavedSearch.price_min,
                    ),
                    or_(
                        SavedSearch.price_max.is_(None),
                        Listing.unit_price_santim <= SavedSearch.price_max,
                    ),
                    or_(
                        SavedSearch.harvest_date_from.is_(None),
                        Listing.harvest_date >= SavedSearch.harvest_date_from,
                    ),
                    or_(
                        SavedSearch.harvest_date_to.is_(None),
                        Listing.harvest_date <= SavedSearch.harvest_date_to,
                    ),
                    or_(SavedSearch.organic.is_(None), Listing.organic == SavedSearch.organic),
                    or_(
                        SavedSearch.delivery_option.is_(None),
                        Listing.delivery_options.any(cast(Any, SavedSearch.delivery_option)),
                    ),
                    or_(
                        SavedSearch.seller_tier_min.is_(None),
                        seller_tier_rank >= _MINIMUM_TIER_RANK,
                    ),
                    or_(
                        SavedSearch.radius_km.is_(None),
                        ~owner_has_point,
                        distance <= SavedSearch.radius_km * 1_000,
                    ),
                ),
            )
            .join(Woreda, Woreda.id == Listing.woreda_id)
            .join(owner, owner.id == SavedSearch.user_id)
            .join(seller, seller.id == Listing.seller_id)
            .where(
                SavedSearch.id.in_(list(since_map)),
                SavedSearch.digest.is_(True),
                owner.status == AccountStatus.ACTIVE,
                _on_the_market(now),
                Listing.created_at > since,
            )
        )

        ranked = matching.subquery("ranked_saved_search_matches")
        capped = (
            select(
                ranked.c.search_id,
                func.max(ranked.c.new_listing_count).label("new_listing_count"),
                func.array_agg(ranked.c.listing_id).label("listing_ids"),
            )
            .where(ranked.c.position <= limit)
            .group_by(ranked.c.search_id)
            .subquery("capped_saved_search_matches")
        )

        result = await self._session.execute(
            select(
                SavedSearch,
                owner,
                Crop,
                Region,
                capped.c.new_listing_count,
                capped.c.listing_ids,
            )
            .join(capped, capped.c.search_id == SavedSearch.id)
            .join(owner, owner.id == SavedSearch.user_id)
            .outerjoin(Crop, Crop.id == SavedSearch.crop_id)
            .outerjoin(Region, Region.code == SavedSearch.region_code)
            .order_by(SavedSearch.created_at.asc(), SavedSearch.id.asc())
        )

        digests: list[SavedSearchDigest] = []
        for search, user, crop, region, count, listing_ids in result.all():
            digests.append(
                SavedSearchDigest(
                    search=search,
                    user=user,
                    crop=crop,
                    region=region,
                    new_listing_count=int(count),
                    new_listing_ids=tuple(listing_ids or ()),
                )
            )
        return digests

    async def mark_digested(self, search_ids: list[UUID], moment: datetime) -> None:
        """Advance these searches' windows to the moment their digest was queued.

        Only the searches the digest actually mentioned are advanced. A search with no new
        listings has nothing to reset, and leaving its window where it was is what lets the
        next run tell the trader about listings that arrived while this one was quiet.
        """
        if not search_ids:
            return
        await self._session.execute(
            update(SavedSearch).where(SavedSearch.id.in_(search_ids)).values(last_digest_at=moment)
        )

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()


def _on_the_market(now: datetime) -> Any:
    """What the market may show: an active listing still in time, or a sold-out one.

    This mirrors the search endpoint's visibility predicate rather than importing it from
    that repository. The two statements have different shapes and a shared private helper would
    make a change to one silently change the digest's definition of a listing a trader may
    be told about.
    """
    return or_(
        and_(Listing.status == ListingStatus.ACTIVE, Listing.expires_at > now),
        Listing.status == ListingStatus.SOLD_OUT,
    )
