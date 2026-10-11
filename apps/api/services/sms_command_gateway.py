"""The application-side implementation of what the SMS dispatcher asks for.

The grammar and the reply choices live in ``packages.shared.messaging.dispatcher``, which
knows a trader, a listing, an offer and an order only as small records and knows nothing of a
session, a repository or the web framework. This class is the other half of that seam: it
holds the session and the services the HTTP paths already use, and it answers each of the
dispatcher's questions in domain terms.

The reason for the split is not tidiness. The shared package must not import the application,
and the grammar is worth asserting without a database; a dispatcher that could reach a session
would be a dispatcher that had to be started to be tested. Everything the SMS path does to
the platform goes through the same services as the API, so a listing posted by text is the
same listing, held to the same rules, as one posted from the app.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any
from uuid import UUID

from apps.api.core.config import Settings
from apps.api.models.crop import Crop
from apps.api.models.listing import Listing as ListingRow
from apps.api.repositories.action_token_repository import ActionTokenRepository
from apps.api.repositories.crop_repository import CropRepository
from apps.api.repositories.listing_repository import ListingRepository
from apps.api.repositories.offer_repository import OfferRepository
from apps.api.repositories.order_repository import OrderRepository
from apps.api.repositories.price_index_repository import PriceIndexRepository
from apps.api.repositories.profile_repository import ProfileRepository
from apps.api.repositories.user_repository import UserRepository
from apps.api.services.adapter_offer_mode import adapter_offer_payment_mode
from apps.api.services.listing_service import ListingService
from apps.api.services.offer_service import OfferService
from apps.api.services.offer_service import effective_status as effective_offer_status
from apps.api.services.order_service import OrderService
from packages.shared.errors import ApiError
from packages.shared.i18n.languages import Language
from packages.shared.messaging.action_tokens import hash_token
from packages.shared.messaging.dispatcher import (
    Listing,
    Offer,
    Order,
    Trader,
)

logger = logging.getLogger("gebeyalink.api")

#: Which crop column answers for each language. ti is absent because no account reads it yet;
#: a language that turns up without a column falls back to the English name.
_NAME_COLUMNS: dict[Language, str] = {
    Language.AM: "name_am",
    Language.OM: "name_om",
    Language.EN: "name_en",
}


class SmsCommandGateway:
    """The dispatcher's dependencies, answered from the application's own services."""

    def __init__(
        self,
        *,
        users: UserRepository,
        profiles: ProfileRepository,
        crops: CropRepository,
        price_indices: PriceIndexRepository,
        listings: ListingRepository,
        offers: OfferRepository,
        orders: OrderRepository,
        action_tokens: ActionTokenRepository,
        listing_service: ListingService,
        offer_service: OfferService,
        order_service: OrderService,
        settings: Settings,
    ) -> None:
        self._users = users
        self._profiles = profiles
        self._crops = crops
        self._price_indices = price_indices
        self._listings = listings
        self._offers = offers
        self._orders = orders
        self._action_tokens = action_tokens
        self._listing_service = listing_service
        self._offer_service = offer_service
        self._order_service = order_service
        self._settings = settings

    async def resolve_trader(self, phone_e164: str) -> Trader | None:
        """The sender, if this number is registered, with the profile the reply needs.

        A number with no account is not an error: it is a person texting a short code they
        have not registered for, and the answer is to point them at registration rather than
        to refuse the message.
        """
        user = await self._users.find_by_phone(phone_e164)
        if user is None:
            return None
        profile = await self._profiles.find_by_user_id(user.id)
        return Trader(
            id=str(user.id),
            language=user.language,
            region_code=profile.region_code if profile else None,
            woreda_id=profile.woreda_id if profile else None,
            kebele=profile.kebele if profile else None,
            business_name=profile.business_name if profile else None,
            display_name=profile.display_name if profile else None,
        )

    async def crop_id_for_name(self, name: str) -> int | None:
        crop = await self._crops.find_by_name(name)
        return crop.id if crop is not None else None

    async def crop_display_name(self, crop_id: int, language: Language) -> str | None:
        crop = await self._crops.find_by_id(crop_id)
        if crop is None:
            return None
        return _crop_name(crop, language)

    async def latest_price_santim(
        self, crop_id: int, region_code: str | None, as_of: date
    ) -> int | None:
        """The published average for this crop and region, or nothing to quote.

        No region means no price: the index is per (crop, region), and a figure with no
        region under it would be a price nobody published. An index that recorded too little
        data to judge is treated the same way, because a median of two sales is not a price.
        """
        if region_code is None:
            return None
        index = await self._price_indices.latest_for(crop_id, region_code, as_of)
        if index is None or index.insufficient_data or index.avg_unit_price_santim is None:
            return None
        return index.avg_unit_price_santim

    async def find_listing_by_short_code(self, short_code: str) -> Listing | None:
        row = await self._listings.find_by_short_code(short_code)
        return _listing(row) if row is not None else None

    async def find_listing_by_id(self, listing_id: str) -> Listing | None:
        loaded = await self._listings.find_by_id(_as_uuid(listing_id))
        return _listing(loaded.listing) if loaded is not None else None

    async def find_offer(self, offer_id: str) -> Offer | None:
        context = await self._offers.find_in_context(_as_uuid(offer_id))
        if context is None:
            return None
        return Offer(
            id=str(context.offer.id),
            listing_id=str(context.offer.listing_id),
            buyer_id=str(context.offer.buyer_id),
            price_santim=context.offer.price_santim,
            status=effective_offer_status(context.offer).value,
        )

    async def find_order_by_short_code(self, short_code: str, viewer_id: str) -> Order | None:
        row = await self._orders.find_by_short_code_for_party(short_code, _as_uuid(viewer_id))
        if row is None:
            return None
        return await self._order_view(row.id, viewer_id)

    async def find_order_by_id(self, order_id: str, viewer_id: str) -> Order | None:
        return await self._order_view(_as_uuid(order_id), viewer_id)

    async def create_listing(self, seller: Trader, body: dict[str, Any], key: str) -> str:
        response = await self._listing_service.create(_as_uuid(seller.id), body, key=key)
        return response.short_code

    async def place_offer(
        self, listing: Listing, buyer: Trader, quantity_kg: int, price_santim: int, key: str
    ) -> None:
        """Place the bid the trader texted, choosing the mode on their behalf when there is one.

        The grammar has no room for a payment mode, so the adapter takes the choice a handset
        cannot express: `adapter_offer_payment_mode` names the one mode the deployment funds when
        escrow is off, and leaves the field out when escrow is on so the offer's own default
        applies. The trader still chooses weight and price; they never choose this.
        """
        body: dict[str, Any] = {"quantity_kg": quantity_kg, "price_santim": price_santim}
        mode = adapter_offer_payment_mode(self._settings)
        if mode is not None:
            body["payment_mode"] = mode
        await self._offer_service.place(
            _as_uuid(listing.id),
            _as_uuid(buyer.id),
            body,
            key=key,
        )

    async def accept_offer(
        self, offer: Offer, trader: Trader, body: dict[str, Any], key: str
    ) -> None:
        await self._order_service.accept(_as_uuid(offer.id), _as_uuid(trader.id), body, key)

    async def reject_offer(self, offer: Offer, trader: Trader, key: str) -> None:
        await self._offer_service.reject(_as_uuid(offer.id), _as_uuid(trader.id), key=key)

    async def cancel_order(self, order: Order, trader: Trader, key: str) -> None:
        await self._order_service.cancel(_as_uuid(order.id), _as_uuid(trader.id), key)

    async def confirm_delivery(self, order: Order, trader: Trader, key: str) -> None:
        await self._order_service.confirm_delivery(_as_uuid(order.id), _as_uuid(trader.id), key)

    async def consume_action_token(self, token: str) -> str | None:
        """Spend the token and return its purpose, or nothing if it could not be spent.

        The reason a spend failed is logged and not returned: not-found, expired and
        already-used are one sentence to the trader, and the distinction is for whoever reads
        the log later. The token itself never reaches the log.
        """
        row, reason = await self._action_tokens.consume_by_hash(hash_token(token))
        if row is None:
            logger.info("action token refused reason=%s", reason.value)
            return None
        return row.purpose

    async def _order_view(self, order_id: UUID, viewer_id: str) -> Order | None:
        """One order, judged for this viewer, or nothing if they may not see it.

        Read through the order service rather than the repository so that the status is the
        clock-judged one — a shipment past its holding window is delivered to anybody who
        looks, and the SMS path must not read it as still in transit.
        """
        try:
            response = await self._order_service.read(order_id, _as_uuid(viewer_id))
        except ApiError:
            return None
        return Order(
            id=str(response.id),
            short_code=response.short_code,
            status=response.status.value,
            buyer_id=str(response.buyer_id),
            seller_id=str(response.seller_id),
        )


def _listing(row: ListingRow) -> Listing:
    return Listing(
        id=str(row.id),
        seller_id=str(row.seller_id),
        crop_id=row.crop_id,
        short_code=row.short_code,
        delivery_options=tuple(option.value for option in row.delivery_options),
    )


def _crop_name(crop: Crop, language: Language) -> str:
    column = _NAME_COLUMNS.get(language, "name_en")
    return getattr(crop, column, None) or crop.name_en


def _as_uuid(value: str) -> UUID:
    return UUID(value)
