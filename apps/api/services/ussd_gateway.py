"""The application half of the USSD seam: the engine's questions, answered for real.

The engine in ``packages.ussd`` decides which menu is owed and renders it; this class is where
the platform's actual data answers back. It holds the repositories and services the HTTP and
SMS paths already use, so a bid placed by dialling a short code is the same bid, held to the
same rules, as one posted from the app — the listing cap, the thread depth, every guard, all
of it.

Three decisions are stated here rather than left to be inferred.

The prefetch methods are the only places this class reads with a handset waiting. SPEC §14.1
caps every handler at two seconds, so a screen is never rendered by walking a session into a
lazy ORM load per row; each screen's data is read up front and handed to the renderer already
shaped. A price board that genuinely has nothing to show raises a refusal rather than
rendering a phantom, which ends the session with the honest template.

Mutations go through the services under the idempotency key the use of the SMS channel made
explicit. The key is derived as ``{sessionId}:{operation}:{entity}`` — one key per press, per
session, per target — and the services prefix it with the account, so a gateway retry of the
same keypress places one bid, exactly as a replay of one SMS places one bid. The counter is
the deliberate exception: no key on the service, because the press already happened on the
confirm screen and the only retry that could double it is a screen replay, which the
terminal-node rule of the engine already absorbs.

Two channels, two derivations. The SMS channel keys on ``sms:{from_phone}:{sha256(raw_text)}``
because a text is self-contained; the USSD channel keys on the session because a keypress is
meaningless without its menu context. Both are correct for their medium; do not try to unify
them.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from apps.api.core.config import Settings
from apps.api.models.crop import Crop
from apps.api.models.listing import DeliveryOption, Listing, ListingSource, ListingStatus
from apps.api.models.offer import OfferStatus
from apps.api.models.order import Order, OrderStatus
from apps.api.repositories.crop_repository import CropRepository
from apps.api.repositories.listing_repository import ListingRepository
from apps.api.repositories.offer_repository import OfferRepository
from apps.api.repositories.order_repository import OrderRepository
from apps.api.repositories.price_index_repository import PriceIndexRepository
from apps.api.repositories.profile_repository import ProfileRepository
from apps.api.repositories.region_repository import RegionRepository
from apps.api.repositories.search_repository import (
    SearchFilters,
    SearchQuery,
    SearchRepository,
)
from apps.api.repositories.user_repository import UserRepository
from apps.api.repositories.woreda_repository import WoredaRepository
from apps.api.schemas.search import SearchSort
from apps.api.services.adapter_offer_mode import adapter_offer_payment_mode
from apps.api.services.listing_service import ListingService
from apps.api.services.listing_service import effective_status as effective_listing_status
from apps.api.services.offer_service import OfferService
from apps.api.services.offer_service import effective_status as effective_offer_status
from apps.api.services.order_service import OrderService
from apps.api.services.order_service import effective_status as effective_order_status
from packages.shared.errors import ApiError, ErrorCode
from packages.shared.i18n.languages import DEFAULT_LANGUAGE, Language
from packages.shared.messaging.dispatcher import format_etb, parse_etb_to_santim
from packages.shared.messaging.queues import EAT_ZONE
from packages.ussd.engine import UssdRefusalError
from packages.ussd.render import render
from packages.ussd.session import SessionState
from packages.ussd.trees.model import Action

logger = logging.getLogger("gebeyalink.api")

#: How many of a trader's orders a USSD screen shows at most. Bigger than SMS budgets allow,
#: because a menu is a screen rather than a text; still capped, because a phone is a phone.
_ORDERS_PAGE_LIMIT = 20

#: How many crops the create-listing wizard ranks and pages through. The catalogue holds
#: nineteen today; the cap keeps a grown catalogue from being read in full on every keypress.
_NEW_LISTING_CROP_LIMIT = 20

#: How many of those crops one screen shows. The binding limit is the USSD channel's: a crop row
#: carries both the English and the Amharic name, so every crop screen is UCS-2 rather than GSM
#: 7-bit whatever language the trader chose, and the channel is assumed to hold ~80 UCS-2
#: characters. Measured worst-case pages over the catalogue (with the ``CON `` prefix, the
#: "Choose a crop:" heading and the "more" line): four rows 94-102 characters, three rows 85-88,
#: two rows 66-74. Two is the largest page that fits. Africa's Talking's documented per-screen
#: limit is the open question (O-2) that confirms or adjusts this number.
_NEW_LISTING_CROP_PAGE = 2

#: How many crop rows the buyer's browse menu shows per screen. The constraint is the channel's
#: and not the list's, so it is the same one the wizard's crop menu is held to: a crop row can
#: carry Ge'ez, which makes the screen UCS-2, and only two rows fit beside the heading and the
#: "more" line. ``test_ussd_screen_length`` holds this page to that budget.
_BROWSE_CROP_PAGE = 2

#: What a one-line prompt can honestly mean by "quantity" and "price". A million kilos is the
#: listing cap itself; a million birr a kilo is past any crop this market trades. The wizard
#: refuses anything above these on the amount screen, where the trader can still correct it.
_MAX_LISTING_QUANTITY_KG = 1_000_000
_MAX_UNIT_PRICE_ETB = 1_000_000

#: Orders a dialling trader cares about, grouped by what is happening to them (§14.3 Path 3).
_AWAITING_STATUSES = frozenset({OrderStatus.CREATED, OrderStatus.AWAITING_FUNDING})
_IN_PROGRESS_STATUSES = frozenset(
    {
        OrderStatus.CONFIRMED,
        OrderStatus.FUNDED,
        OrderStatus.READY_FOR_PICKUP,
        OrderStatus.SHIPPED,
        OrderStatus.DISPUTED,
    }
)
_COMPLETED_STATUSES = frozenset({OrderStatus.DELIVERED, OrderStatus.COMPLETED})

#: Which crop column answers for each language, mirroring the SMS gateway's choice.
_NAME_COLUMNS: dict[Language, str] = {
    Language.AM: "name_am",
    Language.OM: "name_om",
    Language.EN: "name_en",
}

_LANGUAGE_DIGITS: dict[str, Language] = {
    "1": Language.AM,
    "2": Language.EN,
    "3": Language.OM,
}


class UssdGateway:
    """Everything the engine needs to know, answered from the API's own services."""

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
        regions: RegionRepository,
        woredas: WoredaRepository,
        search: SearchRepository,
        offer_service: OfferService,
        order_service: OrderService,
        listing_service: ListingService,
        settings: Settings,
    ) -> None:
        self._users = users
        self._profiles = profiles
        self._crops = crops
        self._price_indices = price_indices
        self._listings = listings
        self._offers = offers
        self._orders = orders
        self._regions = regions
        self._woredas = woredas
        self._search = search
        self._offer_service = offer_service
        self._order_service = order_service
        self._listing_service = listing_service
        self._settings = settings

    # -- who is dialling ----------------------------------------------------

    async def resolve_user(self, phone_e164: str) -> str | None:
        user = await self._users.find_by_phone(phone_e164)
        return str(user.id) if user is not None else None

    async def user_language(self, user_id: str) -> Language:
        user = await self._users.find_by_id(_as_uuid(user_id))
        return user.language if user is not None else DEFAULT_LANGUAGE

    # -- reading what a screen needs ----------------------------------------

    async def prefetch(self, name: str, state: SessionState) -> dict[str, Any]:
        handler = getattr(self, f"_prefetch_{name}", None)
        if handler is None:  # pragma: no cover - guarded by the tree build
            raise ValueError(f"unknown prefetch {name!r}")
        return dict(await handler(state))

    async def _prefetch_listing_counts(self, state: SessionState) -> dict[str, Any]:
        assert state.user_id is not None
        seller_id = _as_uuid(state.user_id)
        loaded, _ = await self._listings.page_for_seller(seller_id, None, 100)
        active = sum(
            1 for row in loaded if effective_listing_status(row.listing) is ListingStatus.ACTIVE
        )
        paused = sum(
            1 for row in loaded if effective_listing_status(row.listing) is ListingStatus.PAUSED
        )
        return {"active_count": str(active), "paused_count": str(paused)}

    async def _prefetch_new_listing_crops(self, state: SessionState) -> dict[str, Any]:
        """One page of the create-listing wizard's crop menu, ordered by what is listed.

        The row is ``{n}. {name_en} {name_am}``: both names, because a one-line menu can say
        what a sentence cannot. The pages are short for a reason that belongs to the channel
        rather than to the list: a row carries Ge'ez, so the whole screen is UCS-2 and only two
        rows fit the budget (see ``_NEW_LISTING_CROP_PAGE``). When more crops follow, the page
        ends with a numbered "more" row whose number the move resolves; ``crop_ids`` carries
        only the crops this page shows, so a press is an index into what is on the screen.
        """
        offset = max(0, int(state.context.get("offset", 0)))
        crops = await self._crops.list_top_by_listed_volume(_NEW_LISTING_CROP_LIMIT)
        page = list(crops[offset : offset + _NEW_LISTING_CROP_PAGE])
        lines = [
            f"{index}. {crop.name_en} {crop.name_am}" for index, crop in enumerate(page, start=1)
        ]
        has_more = offset + _NEW_LISTING_CROP_PAGE < len(crops)
        more_key = len(page) + 1
        if has_more:
            lines.append(f"{more_key}. " + render("ussd_new_listing_more", state.lang, {}))
        return {
            "rows": "\n".join(lines),
            "crop_ids": [crop.id for crop in page],
            "has_more_crops": has_more,
            "crop_more_key": str(more_key),
        }

    async def _prefetch_active_listings(self, state: SessionState) -> dict[str, Any]:
        return await self._seller_listing_rows(state, ListingStatus.ACTIVE)

    async def _prefetch_paused_listings(self, state: SessionState) -> dict[str, Any]:
        return await self._seller_listing_rows(state, ListingStatus.PAUSED)

    async def _seller_listing_rows(
        self, state: SessionState, wanted: ListingStatus
    ) -> dict[str, Any]:
        assert state.user_id is not None
        seller_id = _as_uuid(state.user_id)
        loaded, _ = await self._listings.page_for_seller(seller_id, None, 50)
        chosen = [row.listing for row in loaded if effective_listing_status(row.listing) is wanted]
        lines: list[str] = []
        listing_ids: list[str] = []
        for index, listing in enumerate(chosen, start=1):
            name = await self._crop_name(listing.crop_id, state.lang)
            lines.append(
                f"{index}. "
                + render(
                    "ussd_listing_row",
                    state.lang,
                    {
                        "code": listing.short_code,
                        "crop": name,
                        "quantity_kg": f"{listing.quantity_kg:,}",
                        "price": format_etb(listing.unit_price_santim),
                    },
                )
            )
            listing_ids.append(str(listing.id))
        if not lines:
            lines = [render("ussd_no_listings", state.lang, {})]
        return {"rows": "\n".join(lines), "listing_ids": listing_ids}

    async def _prefetch_offers_for_selected(self, state: SessionState) -> dict[str, Any]:
        listing_id = _as_uuid(state.context["selected_listing_id"])
        offers = await self._offers.list_for_listing(listing_id)
        open_offers = [
            offer for offer in offers if effective_offer_status(offer) is OfferStatus.OPEN
        ]
        lines: list[str] = []
        offer_ids: list[str] = []
        offer_qtys: list[str] = []
        offer_prices: list[str] = []
        for index, offer in enumerate(open_offers, start=1):
            lines.append(
                f"{index}. "
                + render(
                    "ussd_offer_line",
                    state.lang,
                    {
                        "quantity_kg": f"{offer.quantity_kg:,}",
                        "price": format_etb(offer.price_santim),
                    },
                )
            )
            offer_ids.append(str(offer.id))
            offer_qtys.append(str(offer.quantity_kg))
            offer_prices.append(str(offer.price_santim))
        if not lines:
            lines = [render("ussd_no_results", state.lang, {})]
        return {
            "code": str(state.context.get("selected_listing_code") or ""),
            "rows": "\n".join(lines),
            "offer_ids": offer_ids,
            "offer_qtys": offer_qtys,
            "offer_prices": offer_prices,
        }

    async def _prefetch_browse_regions(self, state: SessionState) -> dict[str, Any]:
        regions = await self._regions.list_all()
        lines = [
            f"{index}. {_region_name(region)}" for index, region in enumerate(regions, start=1)
        ]
        last_used = await self._last_used_region(state)
        return {
            "rows": "\n".join(lines),
            "region_codes": [region.code for region in regions],
            "last_used": last_used,
        }

    async def _prefetch_browse_crops(self, state: SessionState) -> dict[str, Any]:
        """One page of the buyer's crop menu, in the order the catalogue is published in.

        The screen is the wizard's screen with a different list behind it, so it is held to the
        same channel budget: a row can carry Ge'ez, and only two fit (see ``_BROWSE_CROP_PAGE``).
        When more crops follow, the page ends with a numbered "more" row whose number the move
        resolves; ``crop_ids`` carries only the crops this page shows, so a press is an index
        into what is on the screen and a crop picked from a later page resolves to that page's
        crop rather than to whatever sat at that number in the whole list.
        """
        offset = max(0, int(state.context.get("offset", 0)))
        crops = await self._crops.list_active_kg_crops()
        page = list(crops[offset : offset + _BROWSE_CROP_PAGE])
        lines = [
            f"{index}. {_crop_name(crop, state.lang)}" for index, crop in enumerate(page, start=1)
        ]
        has_more = offset + _BROWSE_CROP_PAGE < len(crops)
        more_key = len(page) + 1
        if has_more:
            lines.append(f"{more_key}. " + render("ussd_new_listing_more", state.lang, {}))
        return {
            "rows": "\n".join(lines),
            "crop_ids": [crop.id for crop in page],
            "has_more_crops": has_more,
            "crop_more_key": str(more_key),
        }

    async def _prefetch_browse_results(self, state: SessionState) -> dict[str, Any]:
        region_code = state.context.get("region_code")
        crop_id = (
            int(state.context["crop_id"]) if state.context.get("crop_id") is not None else None
        )
        offset = int(state.context.get("offset", 0))
        buyer_id = _as_uuid(state.user_id) if state.user_id else None
        query = SearchQuery(
            filters=SearchFilters(crop_id=crop_id, region_code=region_code),
            sort=SearchSort.DISTANCE_ASC,
            after=None,
            limit=offset + 3,
            buyer_id=buyer_id,
        )
        page, has_more = await self._search.page(query)
        page_rows = page[offset : offset + 3]

        woredas = {
            woreda.id: woreda.name for woreda in await self._woredas.list_by_region(region_code)
        }
        lines: list[str] = []
        listing_ids: list[str] = []
        places: list[str] = []
        distances: list[str] = []
        for index, row in enumerate(page_rows, start=offset + 1):
            listing = row.listing
            place = woredas.get(listing.woreda_id, "")
            distance = ""
            if row.distance_m is not None and row.distance_m >= 1000:
                distance = f" ({round(row.distance_m / 1000)}km)"
            name = await self._crop_name(listing.crop_id, state.lang)
            lines.append(
                f"{index}. "
                + render(
                    "ussd_result_detail",
                    state.lang,
                    {
                        "crop": name,
                        "quantity_kg": f"{listing.quantity_kg:,}",
                        "price": format_etb(listing.unit_price_santim),
                        "place": place,
                        "distance": distance,
                    },
                )
            )
            listing_ids.append(str(listing.id))
            places.append(place)
            distances.append(distance)
        if not lines:
            lines = [render("ussd_no_results", state.lang, {})]
        return {
            "rows": "\n".join(lines),
            "listing_ids": listing_ids,
            "listing_places": places,
            "listing_distances": distances,
            "has_more": has_more,
        }

    async def _prefetch_browse_result_detail(self, state: SessionState) -> dict[str, Any]:
        listing_id = _as_uuid(state.context["selected_listing_id"])
        loaded = await self._listings.find_by_id(listing_id)
        if loaded is None:
            raise UssdRefusalError("ussd_no_results")
        listing = loaded.listing
        index = int(state.context.get("selected_listing_index", 0))
        places = state.context.get("listing_places")
        distances = state.context.get("listing_distances")
        place = (
            places[index]
            if isinstance(places, list) and -len(places) <= index < len(places)
            else ""
        )
        distance = (
            distances[index]
            if isinstance(distances, list) and -len(distances) <= index < len(distances)
            else ""
        )
        name = await self._crop_name(listing.crop_id, state.lang)
        detail = {
            "crop": name,
            "quantity_kg": f"{listing.quantity_kg:,}",
            "price": format_etb(listing.unit_price_santim),
            "place": place,
            "distance": distance,
        }
        next_line = "2. Next page" if state.context.get("has_more") else ""
        return {**detail, "next": next_line}

    async def _prefetch_orders_awaiting(self, state: SessionState) -> dict[str, Any]:
        return self._order_group_rows(state, _AWAITING_STATUSES, await self._party_orders(state))

    async def _prefetch_orders_in_progress(self, state: SessionState) -> dict[str, Any]:
        return self._order_group_rows(state, _IN_PROGRESS_STATUSES, await self._party_orders(state))

    async def _prefetch_orders_completed(self, state: SessionState) -> dict[str, Any]:
        return self._order_group_rows(state, _COMPLETED_STATUSES, await self._party_orders(state))

    async def _party_orders(self, state: SessionState) -> list[Order]:
        """The trader's orders from either side of the table, newest first, deduplicated."""
        assert state.user_id is not None
        party_id = _as_uuid(state.user_id)
        bought = (
            await self._orders.page_for_party(
                party_id, buyer=True, status=None, after=None, limit=_ORDERS_PAGE_LIMIT
            )
        ).orders
        sold = (
            await self._orders.page_for_party(
                party_id, buyer=False, status=None, after=None, limit=_ORDERS_PAGE_LIMIT
            )
        ).orders
        seen: dict[UUID, Order] = {}
        for order in [*bought, *sold]:
            seen.setdefault(order.id, order)
        return sorted(seen.values(), key=lambda order: order.created_at, reverse=True)

    def _order_group_rows(
        self, state: SessionState, statuses: frozenset[OrderStatus], orders: list[Order]
    ) -> dict[str, Any]:
        assert state.user_id is not None
        party_id = _as_uuid(state.user_id)
        grouped = [order for order in orders if effective_order_status(order) in statuses][
            :_ORDERS_PAGE_LIMIT
        ]
        lines: list[str] = []
        order_ids: list[str] = []
        order_roles: list[str] = []
        order_codes: list[str] = []
        for index, order in enumerate(grouped, start=1):
            lines.append(
                f"{index}. "
                + render(
                    "ussd_order_row",
                    state.lang,
                    {
                        "code": order.short_code,
                        "quantity_kg": f"{order.quantity_kg:,}",
                        "status": order.status.value,
                    },
                )
            )
            order_ids.append(str(order.id))
            order_roles.append("buyer" if order.buyer_id == party_id else "seller")
            order_codes.append(order.short_code)
        if not lines:
            lines = [render("ussd_no_orders", state.lang, {})]
        return {
            "rows": "\n".join(lines),
            "order_ids": order_ids,
            "order_roles": order_roles,
            "order_codes": order_codes,
        }

    async def _prefetch_order_buyer_detail(self, state: SessionState) -> dict[str, Any]:
        order = await self._orders.find_by_id(_as_uuid(state.context["selected_order_id"]))
        if order is None:
            raise UssdRefusalError("ussd_no_orders")
        return {
            "order_code": order.short_code,
            "pickup_code": order.pickup_code or "",
        }

    async def _prefetch_price_board(self, state: SessionState) -> dict[str, Any]:
        region_code: str | None = None
        if state.user_id is not None:
            profile = await self._profiles.find_by_user_id(_as_uuid(state.user_id))
            region_code = profile.region_code if profile is not None else None
        if region_code is None:
            raise UssdRefusalError("ussd_price_unavailable")
        region = await self._regions.find(region_code)
        region_name = _region_name(region) if region is not None else region_code
        today = datetime.now(UTC).date()
        parts: list[str] = []
        crops = await self._crops.list_active_kg_crops()
        for crop in crops:
            index = await self._price_indices.latest_for(crop.id, region_code, today)
            if (
                index is not None
                and not index.insufficient_data
                and index.avg_unit_price_santim is not None
            ):
                parts.append(
                    render(
                        "ussd_price_row",
                        state.lang,
                        {
                            "crop": _crop_name(crop, state.lang),
                            # The board stores the per-kilogram price in santim and the row
                            # quotes birr per quintal; the two are the same figure, because a
                            # hundred kilograms to the quintal cancels a hundred santim to the
                            # birr. Dividing here would quote a hundredth of what the board
                            # published, which is the same mistake the SMS price reply made.
                            "price": f"{index.avg_unit_price_santim:,}",
                        },
                    )
                )
                if len(parts) >= 5:
                    break
        if not parts:
            raise UssdRefusalError("ussd_price_unavailable")
        return {"region": region_name, "prices": "; ".join(parts)}

    # -- doing what a choice asks -------------------------------------------

    async def apply(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        handler = getattr(self, f"_op_{action.operation}", None)
        if handler is None:  # pragma: no cover - guarded by the tree build
            raise ValueError(f"unknown operation {action.operation!r}")
        return dict(await handler(action, text, state, session_id))

    async def _op_set_language(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        chosen = _LANGUAGE_DIGITS.get(text.strip())
        if chosen is None:
            raise ApiError(ErrorCode.VALIDATION_ERROR)
        return {"lang": chosen}

    async def _op_select_listing(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        listing_id = self._picked(state, "listing_ids", text)
        loaded = await self._listings.find_by_id(_as_uuid(listing_id))
        context: dict[str, Any] = {
            "selected_listing_id": listing_id,
            "selected_listing_code": (loaded.listing.short_code if loaded is not None else ""),
            "selected_listing_index": self._picked_index(state, "listing_ids", text),
        }
        index = context["selected_listing_index"]
        places = state.context.get("listing_places")
        distances = state.context.get("listing_distances")
        if isinstance(places, list) and -len(places) <= index < len(places):
            context["selected_listing_place"] = places[index]
            context["selected_listing_distance"] = (
                distances[index] if isinstance(distances, list) else ""
            )
        return context

    async def _op_select_offer(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        offer_id = self._picked(state, "offer_ids", text)
        qtys = state.context.get("offer_qtys") or []
        prices = state.context.get("offer_prices") or []
        index = self._picked_index(state, "offer_ids", text)
        return {
            "selected_offer_id": offer_id,
            "selected_offer_qty": qtys[index] if -len(qtys) <= index < len(qtys) else "",
            "selected_offer_price_santim": (
                prices[index] if -len(prices) <= index < len(prices) else ""
            ),
        }

    async def _op_select_region(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        return {"region_code": self._picked(state, "region_codes", text)}

    async def _op_select_crop(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        return {
            "crop_id": self._picked(state, "crop_ids", text),
            "offset": 0,
        }

    async def _op_select_order(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        index = self._picked_index(state, "order_ids", text)
        roles = state.context.get("order_roles") or []
        codes = state.context.get("order_codes") or []
        return {
            "selected_order_id": self._picked(state, "order_ids", text),
            "selected_order_role": roles[index] if -len(roles) <= index < len(roles) else "",
            "selected_order_code": codes[index] if -len(codes) <= index < len(codes) else "",
        }

    async def _op_page_next(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        return {"offset": int(state.context.get("offset", 0)) + 3}

    async def _op_page_next_crops(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        """Step the create-listing crop menu to its next page, one page at a time."""
        return {"offset": int(state.context.get("offset", 0)) + _NEW_LISTING_CROP_PAGE}

    async def _op_page_next_browse_crops(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        """Step the buyer's browse crop menu to its next page, one page at a time."""
        return {"offset": int(state.context.get("offset", 0)) + _BROWSE_CROP_PAGE}

    async def _op_set_quantity(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        value = text.strip().replace(",", "")
        return {"quantity_kg": f"{int(value):,}"}

    async def _op_set_price(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        santim = parse_etb_to_santim(text.strip().replace(",", ""))
        if santim is None:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={"fields": {"price": "not_money"}},
            )
        # ``price`` is the birr figure a confirm screen repeats; ``price_santim`` is what a
        # service consumes. Holding both means the registry can render a sentence and the
        # platform can do money arithmetic, each from the key named for it.
        return {"price_santim": santim, "price": format_etb(santim)}

    async def _op_require_listing_profile(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        """The create-listing precondition: the seller's region and woreda must be known.

        A listing carries both and a one-line menu has no step that could ask for them, so a
        seller whose profile is missing one is told before the first question rather than
        after the last. The values are read here and carried in the session so the publish
        does not read the profile a second time.
        """
        assert state.user_id is not None
        profile = await self._profiles.find_by_user_id(_as_uuid(state.user_id))
        if profile is None or not profile.region_code:
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": {"profile": "region"}})
        if profile.woreda_id is None:
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": {"profile": "woreda"}})
        return {"region_code": profile.region_code, "woreda_id": profile.woreda_id}

    async def _op_set_listing_quantity(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        value = int(text.strip().replace(",", ""))
        if not 1 <= value <= _MAX_LISTING_QUANTITY_KG:
            raise ApiError(ErrorCode.VALIDATION_ERROR)
        return {"quantity_kg": value}

    async def _op_set_listing_price(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        etb = int(text.strip().replace(",", ""))
        if not 1 <= etb <= _MAX_UNIT_PRICE_ETB:
            raise ApiError(ErrorCode.VALIDATION_ERROR)
        # The handset types whole birr and no santim, so the stored price is that many hundred
        # santim; ``price_etb`` is kept so a later screen can repeat the figure it was given.
        return {"unit_price_santim": etb * 100, "price_etb": etb}

    async def _op_set_listing_harvest(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        # The four choices are offsets from today on the East Africa Time calendar, not the UTC
        # one: a farmer in Addis answering after 21:00 UTC means tomorrow, and the date the
        # listing carries must be the date they mean.
        offset_days = {"1": 0, "2": 7, "3": 14, "4": 30}.get(text.strip())
        if offset_days is None:
            raise ApiError(ErrorCode.VALIDATION_ERROR)
        harvest = datetime.now(EAT_ZONE).date() + timedelta(days=offset_days)
        return {"harvest_date": harvest.isoformat()}

    async def _op_set_listing_delivery(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        options = {
            "1": [DeliveryOption.PICKUP],
            "2": [DeliveryOption.SELLER_DELIVERY],
            "3": [DeliveryOption.TRANSPORT_PARTNER],
        }.get(text.strip())
        if options is None:
            raise ApiError(ErrorCode.VALIDATION_ERROR)
        return {
            "delivery_options": [option.value for option in options],
            "delivery_fee_santim": 0,
        }

    async def _op_set_listing_delivery_fee(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        fee_etb = int(text.strip().replace(",", ""))
        # A delivery that sells itself short is not a delivery; the fee must be a positive
        # number of birr or the seller is asked again.
        if fee_etb <= 0:
            raise ApiError(ErrorCode.VALIDATION_ERROR)
        return {"delivery_fee_santim": fee_etb * 100}

    async def _op_publish_listing(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        """Write the listing the five screens described, through the service the app uses.

        §14.4's SMS fallback fills what a one-line menu leaves unsaid: a minimum order of one
        kilo, available now, and a thirty-day life. The region, woreda and delivery were
        gathered by earlier screens. The key is the channel's usual
        ``{sessionId}:{operation}:{entity}`` one, so a gateway retry of the final screen
        posts one listing, exactly as a replay of one offer places one bid.
        """
        assert state.user_id is not None
        now = datetime.now(UTC)
        body: dict[str, Any] = {
            "crop_id": int(state.context["crop_id"]),
            "quantity_kg": int(state.context["quantity_kg"]),
            "min_order_kg": 1,
            "unit_price_santim": int(state.context["unit_price_santim"]),
            "harvest_date": state.context["harvest_date"],
            "available_from": now.isoformat(),
            "expires_at": (now + timedelta(days=30)).isoformat(),
            "region_code": state.context["region_code"],
            "woreda_id": int(state.context["woreda_id"]),
            "delivery_options": list(state.context["delivery_options"]),
            "delivery_fee_santim": int(state.context.get("delivery_fee_santim", 0)),
            "description": "",
            "organic": False,
            "photos": [],
            "listing_attributes": {},
        }
        key = f"{session_id}:publish_listing:new"
        listing = await self._listing_service.create(
            _as_uuid(state.user_id), body, key=key, source=ListingSource.USSD
        )
        return {"code": listing.short_code}

    async def _op_offer_place(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        assert state.user_id is not None
        listing_id = _as_uuid(self._entity_ref(state, action))
        body: dict[str, Any] = {
            "quantity_kg": int(state.context["quantity_kg"].replace(",", "")),
            "price_santim": int(state.context["price_santim"]),
        }
        # The tree asks for a weight and a price and has no step for a payment mode, so the
        # adapter takes the choice a one-line menu cannot offer: the one mode the deployment
        # funds when escrow is off, and nothing when escrow is on, leaving the default.
        mode = adapter_offer_payment_mode(self._settings)
        if mode is not None:
            body["payment_mode"] = mode
        key = f"{session_id}:offer_place:{self._entity_ref(state, action)}"
        await self._offer_service.place(listing_id, _as_uuid(state.user_id), body, key=key)
        return {"code": str(state.context.get("selected_listing_code") or "")}

    async def _op_offer_accept(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        assert state.user_id is not None
        offer_id = _as_uuid(self._entity_ref(state, action))
        context = await self._offers.find_in_context(offer_id)
        if context is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "offer"})
        delivery_method = _delivery_method_for(context.listing)
        if delivery_method is None:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={"fields": {"profile": "delivery_method"}},
            )
        buyer_profile = await self._profiles.find_by_user_id(context.offer.buyer_id)
        if buyer_profile is None or buyer_profile.woreda_id is None:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={"fields": {"profile": "woreda"}},
            )
        address = buyer_profile.kebele or buyer_profile.business_name or buyer_profile.display_name
        if not address:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={"fields": {"profile": "address"}},
            )
        body = {
            "delivery_method": delivery_method,
            "buyer_address_woreda_id": buyer_profile.woreda_id,
            "buyer_address_text": address,
        }
        key = f"{session_id}:offer_accept:{self._entity_ref(state, action)}"
        await self._order_service.accept(offer_id, _as_uuid(state.user_id), body, key=key)
        return {}

    async def _op_offer_reject(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        assert state.user_id is not None
        offer_ref = self._entity_ref(state, action)
        key = f"{session_id}:offer_reject:{offer_ref}"
        await self._offer_service.reject(_as_uuid(offer_ref), _as_uuid(state.user_id), key=key)
        return {}

    async def _op_offer_counter(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        assert state.user_id is not None
        offer_id = _as_uuid(self._entity_ref(state, action))
        body = {
            "quantity_kg": int(state.context.get("selected_offer_qty", "0") or 0),
            "price_santim": int(state.context["price_santim"]),
        }
        await self._offer_service.counter(offer_id, _as_uuid(state.user_id), body)
        return {}

    async def _op_delivery_confirm(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        assert state.user_id is not None
        order_ref = self._entity_ref(state, action)
        key = f"{session_id}:delivery_confirm:{order_ref}"
        await self._order_service.confirm_pickup(
            _as_uuid(order_ref),
            _as_uuid(state.user_id),
            {"pickup_code": text},
            key=key,
        )
        return {}

    # -- small helpers --------------------------------------------------------

    async def _crop_name(self, crop_id: int, language: Language | None) -> str:
        crop = await self._crops.find_by_id(crop_id)
        return _crop_name(crop, language or DEFAULT_LANGUAGE) if crop is not None else str(crop_id)

    async def _last_used_region(self, state: SessionState) -> str:
        if state.user_id is None:
            return "-"
        profile = await self._profiles.find_by_user_id(_as_uuid(state.user_id))
        if profile is None or not profile.region_code:
            return "-"
        region = await self._regions.find(profile.region_code)
        return _region_name(region) if region is not None else profile.region_code

    @staticmethod
    def _picked(state: SessionState, key: str, text: str) -> str:
        values = state.context.get(key)
        if not isinstance(values, list):
            raise ApiError(ErrorCode.VALIDATION_ERROR)
        position = int(text.strip()) - 1
        if not 0 <= position < len(values):
            raise ApiError(ErrorCode.VALIDATION_ERROR)
        return str(values[position])

    @staticmethod
    def _picked_index(state: SessionState, key: str, text: str) -> int:
        values = state.context.get(key)
        if not isinstance(values, list):
            raise ApiError(ErrorCode.VALIDATION_ERROR)
        position = int(text.strip()) - 1
        if not 0 <= position < len(values):
            raise ApiError(ErrorCode.VALIDATION_ERROR)
        return position

    @staticmethod
    def _entity_ref(state: SessionState, action: Action) -> str:
        name = action.entity_from
        if name is None:
            raise ApiError(ErrorCode.INTERNAL_ERROR, details={"reason": "missing_entity"})
        value = state.context.get(name)
        return value if isinstance(value, str) else str(value)


def _delivery_method_for(listing: Listing) -> str | None:
    """The order's delivery option: pickup when offered, else the listing's first option."""
    options = list(listing.delivery_options) if listing.delivery_options else []
    if DeliveryOption.PICKUP in options:
        return DeliveryOption.PICKUP.value
    if options:
        return options[0].value
    return None


def _region_name(region: Any) -> str:
    return region.name_en if region is not None else ""


def _crop_name(crop: Crop, language: Language | None) -> str:
    chosen = language or DEFAULT_LANGUAGE
    column = _NAME_COLUMNS.get(chosen, "name_en")
    return getattr(crop, column, None) or crop.name_en


def _as_uuid(value: str) -> UUID:
    return UUID(value)
