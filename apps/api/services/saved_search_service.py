"""Saved searches: the ten-per-user rule and the search filter set, before anything is stored.

The service is where two rules that are about a trader rather than about a query string
live.

The first is the limit. Ten saved searches is a normative cap, and a cap that is checked by
counting before an insert is not a cap if two requests can count at once. The account row is
therefore locked before the count and held through the write, so a double-tapped save button
produces one refusal and ten rows rather than eleven.

The second is the radius. A radius is a distance from somewhere, and a trader who has never
given a point has nowhere for it to be measured from. The one-off search endpoint refuses
that combination rather than silently widening it; a saved search applies the same rule at
the moment it is saved, because otherwise the first digest would be the place a trader
discovered the search they saved had quietly ignored its radius.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from apps.api.models.listing import DeliveryOption
from apps.api.models.notification import NotificationChannel
from apps.api.models.saved_search import SavedSearch
from apps.api.models.user import VerificationTier
from apps.api.repositories.crop_repository import CropRepository
from apps.api.repositories.profile_repository import ProfileRepository
from apps.api.repositories.region_repository import RegionRepository
from apps.api.repositories.saved_search_repository import SavedSearchRepository
from apps.api.repositories.woreda_repository import WoredaRepository
from packages.shared.errors import ApiError, ErrorCode

#: The saved-search cap. Kept here rather than in the router because it is a rule about the
#: account, and the SMS and USSD doors are meant to save searches through the same rule.
MAX_SAVED_SEARCHES = 10

#: The widest crop id the table can hold. Checking the bound before the lookup keeps a body
#: naming an id the column cannot store from reaching PostgreSQL as a type error.
_SMALLINT_MAX = 2**15 - 1


@dataclass(frozen=True, slots=True)
class SavedSearchDraft:
    """The body of a save, after the route has turned it into plain values."""

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
    digest: bool = True
    digest_channel: NotificationChannel = NotificationChannel.SMS


class SavedSearchService:
    """The caller's own searches, and the rules that bound their number."""

    def __init__(
        self,
        searches: SavedSearchRepository,
        profiles: ProfileRepository,
        crops: CropRepository,
        regions: RegionRepository,
        woredas: WoredaRepository,
    ) -> None:
        self._searches = searches
        self._profiles = profiles
        self._crops = crops
        self._regions = regions
        self._woredas = woredas

    async def list(self, user_id: UUID) -> list[SavedSearch]:
        """Every search this account holds, newest first."""
        return await self._searches.list_for_user(user_id)

    async def create(self, user_id: UUID, draft: SavedSearchDraft) -> SavedSearch:
        """Save one search, or refuse the eleventh.

        The account row is locked before the count. The rollback on the way out releases it
        as soon as the refusal is decided rather than when the request's session closes.
        """
        await self._check_references(draft)
        if draft.radius_km is not None and not await self._profiles.has_gps(user_id):
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={"fields": {"radius_km": "requires_your_location"}},
            )

        await self._searches.lock_user(user_id)
        try:
            existing = await self._searches.count_for_user(user_id)
            if existing >= MAX_SAVED_SEARCHES:
                raise ApiError(
                    ErrorCode.LIMIT_EXCEEDED,
                    details={
                        "field": "saved_searches",
                        "limit": MAX_SAVED_SEARCHES,
                    },
                )
            search = SavedSearch(
                user_id=user_id,
                crop_id=draft.crop_id,
                region_code=draft.region_code,
                woreda_id=draft.woreda_id,
                radius_km=draft.radius_km,
                price_min=draft.price_min,
                price_max=draft.price_max,
                harvest_date_from=draft.harvest_date_from,
                harvest_date_to=draft.harvest_date_to,
                organic=draft.organic,
                delivery_option=draft.delivery_option,
                seller_tier_min=draft.seller_tier_min,
                digest=draft.digest,
                digest_channel=draft.digest_channel,
            )
            await self._searches.add(search)
            await self._searches.commit()
            return search
        except Exception:
            await self._searches.rollback()
            raise

    async def _check_references(self, draft: SavedSearchDraft) -> None:
        """Refuse a filter set naming a crop, region or woreda the platform does not hold.

        The foreign keys would refuse the row anyway, but as an integrity error rather than
        an answer a client can act on. Checking before the lock keeps a malformed body from
        holding an account's row while it fails, and names every bad field at once.
        """
        problems: dict[str, str] = {}
        if draft.crop_id is not None and (
            not 1 <= draft.crop_id <= _SMALLINT_MAX
            or await self._crops.find_by_id(draft.crop_id) is None
        ):
            problems["crop_id"] = "not_a_seeded_crop"
        if draft.region_code is not None and not await self._regions.exists(draft.region_code):
            problems["region_code"] = "not_a_seeded_region"
        if draft.woreda_id is not None and await self._woredas.find_by_id(draft.woreda_id) is None:
            problems["woreda_id"] = "not_a_seeded_woreda"
        if problems:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={"fields": problems},
            )

    async def delete(self, user_id: UUID, search_id: UUID) -> bool:
        """Delete one of the caller's searches; a second delete is still a success.

        DELETE is idempotent at this door. A caller that retries after a lost response should
        not be told the second attempt failed, and a search belonging to somebody else is
        indistinguishable from one that never existed, which is the point.
        """
        deleted = await self._searches.delete_for_user(user_id, search_id)
        await self._searches.commit()
        return deleted > 0
