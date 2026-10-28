"""The rules for posting, reading, editing and moving a listing.

A listing is the one place where several separate promises have to hold at once, and most of
this module exists to keep them from quietly contradicting each other.

The status is not a field a request may set. There is exactly one function in this module
that changes a listing's status, it consults the state table before it does, and everything
that reaches a status goes through it — including creation, which is the publish transition
from a draft rather than a field the client names. That is why an endpoint cannot drift into
permitting something the table forbids: there is nowhere else for a status to be written.

Expiry is read against the clock rather than waited for. A listing past its expiry reads as
expired and is treated as expired by anything that tries to move it, without any job having
to run first, so a listing cannot be left on sale because nothing happened to look at it.

A price is flagged and never blocked. Whether a price looks wrong against what that crop
last cost in that region is worth saying, and is not worth refusing a sale over. An absent
index and an index that says it had too little data to judge are both answers of "not
flagged", because neither is evidence of anything.

Editing is guarded by the version the caller last saw. Two phones can hold the same listing,
and the second write wins silently otherwise; here the loser is told, and is handed the row
that beat them so it can reconcile rather than retry blindly.

Editing is also guarded by what has already been sold. FR-1904 puts the availability invariant
in the same transaction that writes the listing, so an edit that would push availability below
zero is refused by name, listing the orders holding the kilos. Open offers are not part of that
judgement, because an offer reserves nothing.

A listing whose availability has reached zero reads as sold out. FR-306's trigger is a fact
about the kilos, and this module derives that fact on the way out rather than trusting a column
that a cancellation, an expiry or an absent cron may have left behind.
"""

from __future__ import annotations

import base64
import logging
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from apps.api.core.idempotency import IdempotencyStore, once
from apps.api.core.idempotency import fingerprint_of as _fingerprint
from apps.api.core.short_codes import CROCKFORD_BASE32
from apps.api.models.listing import (
    DeliveryOption,
    Listing,
    ListingPhoto,
    ListingSource,
    ListingStatus,
)
from apps.api.models.notification import Notification, NotificationChannel, NotificationStatus
from apps.api.models.price_index import PriceIndex
from apps.api.repositories.listing_repository import ListingRepository, LoadedListing
from apps.api.repositories.notification_repository import NotificationRepository
from apps.api.repositories.price_index_repository import PriceIndexRepository
from apps.api.repositories.reservation_repository import ReservationRepository
from apps.api.repositories.user_repository import UserRepository
from apps.api.schemas.listing import ListingResponse, PhotoResponse
from packages.shared.errors import ApiError, ErrorCode
from packages.shared.i18n.languages import Language
from packages.shared.i18n.templates import load_templates
from packages.shared.messaging.queues import SmsPriority, eat_calendar_date, priority_rank

logger = logging.getLogger("gebeyalink.api")

#: Crockford's alphabet, which leaves out I, L, O and U so that a code written down from a
#: text message and read aloud cannot be misheard as a different code. That property is the
#: whole reason this alphabet is used instead of the whole alphabet. It lives with the other
#: code rules rather than here, because an order's code is drawn from the same alphabet and two
#: copies of it would eventually be two alphabets.

#: Six characters, thirty bits, with no prefix. An order's code is prefixed and longer, so
#: the two can be told apart by shape without stripping a marker off one of them.
SHORT_CODE_LENGTH = 6

#: How many codes to try before giving up. Six characters of a thirty-two character alphabet
#: is around a billion values, so exhausting a handful of attempts means something is wrong
#: with the column rather than with the draw.
SHORT_CODE_ATTEMPTS = 8

#: How long a listing lives when the seller does not say. Thirty days is the default the
#: specification gives, and the maximum below is the ceiling on any listing.
DEFAULT_LIFETIME = timedelta(days=30)
MAX_LIFETIME = timedelta(days=90)

#: How far ahead a harvest may be dated. A year is as far as a planting schedule can honestly
#: reach; beyond that the date is a guess dressed as a fact.
MAX_HARVEST_WINDOW = timedelta(days=365)

#: The band a price is judged against, as multiples of what the crop last cost. The edges are
#: inside the band, so a price exactly five times the average is not suspicious.
OUTLIER_LOWER_MULTIPLE = Decimal("0.2")
OUTLIER_UPPER_MULTIPLE = Decimal("5")

#: The only channel that reaches this service today. Recorded on the row rather than accepted
#: from it, so the listing always says how it arrived even when nobody asks.
APP_SOURCE = ListingSource.APP

#: The whole state table, as which statuses may follow which. Absent from a row means the
#: status is terminal: nothing follows it, and nothing needs to.
ALLOWED_TRANSITIONS: dict[ListingStatus, frozenset[ListingStatus]] = {
    ListingStatus.DRAFT: frozenset({ListingStatus.ACTIVE, ListingStatus.CLOSED}),
    ListingStatus.ACTIVE: frozenset(
        {
            ListingStatus.PAUSED,
            ListingStatus.SOLD_OUT,
            ListingStatus.EXPIRED,
            ListingStatus.SUSPENDED,
            ListingStatus.CLOSED,
        }
    ),
    ListingStatus.PAUSED: frozenset(
        {ListingStatus.ACTIVE, ListingStatus.EXPIRED, ListingStatus.CLOSED}
    ),
    ListingStatus.SOLD_OUT: frozenset({ListingStatus.CLOSED}),
    ListingStatus.EXPIRED: frozenset({ListingStatus.CLOSED}),
    ListingStatus.SUSPENDED: frozenset({ListingStatus.ACTIVE, ListingStatus.CLOSED}),
    ListingStatus.CLOSED: frozenset(),
}

#: Statuses that cannot be edited. A sold-out, expired or closed listing is a record of what
#: happened rather than an offer still open, so changing it would be rewriting the past.
READ_ONLY_STATUSES = frozenset(
    {ListingStatus.SOLD_OUT, ListingStatus.EXPIRED, ListingStatus.CLOSED}
)


def transition_listing_status(listing: Listing, target: ListingStatus) -> ListingStatus:
    """Move a listing to a new status, if the state table allows it.

    This is the only function in the platform that writes a listing's status, and it is
    called by everything that moves one: publishing, pausing, resuming, closing, and expiry.
    A caller that has not consulted it has not moved a listing.

    Expiry is folded in here rather than left to its callers, because the rule that a lapsed
    listing has already expired belongs to the status and not to whichever operation happened
    to look at it. Without that, a paused listing past its expiry would still be pausable,
    and two readers could disagree about what state it is in.
    """
    current = effective_status(listing)
    if target not in ALLOWED_TRANSITIONS[current]:
        logger.warning(
            "refused listing transition listing_id=%s from=%s to=%s",
            listing.id,
            current.value,
            target.value,
        )
        raise ApiError(
            ErrorCode.ILLEGAL_STATE_TRANSITION,
            details={
                "listing_id": str(listing.id),
                "status": current.value,
                "requested": target.value,
            },
        )
    listing.status = target
    return target


def effective_status(listing: Listing, now: datetime | None = None) -> ListingStatus:
    """The status a listing has when the clock is consulted, rather than when it was written.

    A listing past its expiry is expired, whether or not anything has got round to writing
    that down. Deriving it rather than storing it is what stops an expired listing from
    staying on sale because no job ran, and it means a read never has to write to answer
    honestly.
    """
    moment = now or datetime.now(UTC)
    if listing.status in (ListingStatus.ACTIVE, ListingStatus.PAUSED) and _has_lapsed(
        listing, moment
    ):
        return ListingStatus.EXPIRED
    return listing.status


def status_for_availability(
    listing: Listing, available_kg: int, now: datetime | None = None
) -> ListingStatus:
    """A listing's status with FR-306's trigger applied, from availability rather than a column.

    A listing whose committed kilos have reached its quantity reads as sold out. The sale that
    took the last of it also writes `SOLD_OUT`, so this is a second door onto the same answer
    rather than a competing definition of it - and it is needed because the write is not the only
    way availability can reach zero: a cancellation hands kilos back, an expiry releases a hold,
    and the cron FR-306 asks for does not exist yet.

    Only from `ACTIVE`, because the state table has no edge into `SOLD_OUT` from anywhere else.
    A paused listing with nothing left stays paused, which is right: pausing is the seller's own
    statement about whether the listing is for sale, and a system transition must not overrule
    it.
    """
    status = effective_status(listing, now=now)
    if status is ListingStatus.ACTIVE and available_kg <= 0:
        return ListingStatus.SOLD_OUT
    return status


def is_price_outlier(unit_price_santim: int, average_santim: int | None) -> bool:
    """Whether a price is far enough from what that crop last cost to be worth flagging.

    Takes the average rather than the row it came from, so the arithmetic can be read and
    tested on its own. No average means no flag: absence of evidence is not evidence, and an
    index that has not been filled in must not make every listing look unusual.
    """
    if average_santim is None:
        return False
    price = Decimal(unit_price_santim)
    average = Decimal(average_santim)
    return price < average * OUTLIER_LOWER_MULTIPLE or price > average * OUTLIER_UPPER_MULTIPLE


def _render_listing_created(language: Language, *, code: str) -> tuple[str, str]:
    """The confirmation AC-3.1 owes a seller, and the language it was written in.

    Rendered from the registry like every other message, with the same fallback: a language
    the template does not carry reads the English one. The second value is that language
    rather than the one asked for, because the row stores the language of the words it holds,
    and those are two different questions when the fallback fires.
    """
    template = load_templates()["listing_created"]
    rendered_language = language.value if language.value in template.variants else Language.EN.value
    return template.variants[rendered_language].format(code=code), rendered_language


@dataclass(frozen=True, slots=True)
class ListingValues:
    """Every field a listing's shape is judged by, at one moment.

    Both creation and editing resolve to one of these before anything is written, so a merged
    edit is held to exactly the rules a new listing is.
    """

    crop_id: int
    quantity_kg: int
    min_order_kg: int
    unit_price_santim: int
    harvest_date: date
    available_from: datetime | None
    expires_at: datetime
    region_code: str
    woreda_id: int
    delivery_options: tuple[DeliveryOption, ...]
    delivery_fee_santim: int
    description: str | None
    organic: bool
    photos: tuple[tuple[str, int, int], ...]
    listing_attributes: dict[str, Any]

    def as_column_values(self) -> dict[str, Any]:
        """The subset the listings table itself holds."""
        return {
            "crop_id": self.crop_id,
            "quantity_kg": self.quantity_kg,
            "min_order_kg": self.min_order_kg,
            "unit_price_santim": self.unit_price_santim,
            "harvest_date": self.harvest_date,
            "available_from": self.available_from,
            "expires_at": self.expires_at,
            "region_code": self.region_code,
            "woreda_id": self.woreda_id,
            "delivery_options": list(self.delivery_options),
            "delivery_fee_santim": self.delivery_fee_santim,
            "description": self.description,
            "organic": self.organic,
            "listing_attributes": self.listing_attributes,
        }


class ListingService:
    def __init__(
        self,
        listings: ListingRepository,
        price_indices: PriceIndexRepository,
        reservations: ReservationRepository,
        idempotency: IdempotencyStore,
        users: UserRepository,
        notifications: NotificationRepository,
    ) -> None:
        self._listings = listings
        self._price_indices = price_indices
        self._reservations = reservations
        self._idempotency = idempotency
        self._users = users
        self._notifications = notifications

    async def create(
        self,
        seller_id: UUID,
        body: dict[str, Any],
        key: str | None = None,
        *,
        source: ListingSource = APP_SOURCE,
    ) -> ListingResponse:
        """Post a listing, live, under a code nobody else holds.

        ``key`` is the SMS channel's idempotency key and is absent on the app path, which has
        no way to resend a creation it did not see an answer to. When it is present, the whole
        creation runs under it, so a webhook retried by the gateway posts one listing.

        ``source`` is the door the listing came in through. The app path never says, and the
        SMS path still does not, so both keep the default; the USSD wizard passes the channel
        it is, because the row is the only place that answers how a listing arrived.
        """
        if key is None:
            return await self._create(seller_id, body, source)
        return await once(
            self._idempotency,
            account_id=str(seller_id),
            key=key,
            fingerprint=_fingerprint("listing", body),
            work=lambda: self._create(seller_id, body, source),
            encode=lambda response: (201, response.model_dump(mode="json")),
            decode=ListingResponse.model_validate,
        )

    async def create_on_behalf(
        self,
        agent_id: UUID,
        farmer_id: UUID,
        body: dict[str, Any],
    ) -> ListingResponse:
        """Post a listing as the farmer it belongs to, at an agent's hand.

        The authorization is not decided here. By the time this is reached the agent service
        has already found an active assignment between the two, and this method's job is to
        write the row the way that relationship describes it: the seller is the *farmer*, and
        ``on_behalf_of_user_id`` records whose behalf it was posted on. An agent's own id
        never reaches this call as the seller, so no listing can end up owned by an agent
        merely because an agent typed it.

        It goes through ``_create``, the same path every other door uses, so a listing
        posted on a farmer's behalf is held to every rule a listing is — including the price
        judgement and the publish transition — rather than being a second, looser way in.
        """
        return await self._create(
            farmer_id,
            body,
            ListingSource.AGENT,
            on_behalf_of_user_id=farmer_id,
        )

    async def _create(
        self,
        seller_id: UUID,
        body: dict[str, Any],
        source: ListingSource = APP_SOURCE,
        *,
        on_behalf_of_user_id: UUID | None = None,
    ) -> ListingResponse:
        now = datetime.now(UTC)
        values = self._values_from(
            _with_sms_defaults(body, now), expires_at_default=_default_expiry(now)
        )
        validate_listing_values(values, now=now)

        listing = Listing(
            seller_id=seller_id,
            on_behalf_of_user_id=on_behalf_of_user_id,
            source=source,
            status=ListingStatus.DRAFT,
            short_code=await self._allocate_short_code(),
            price_outlier=await self._judge_price(values, now=now),
            # The first version of a row is one, not the column's default of zero: version
            # counts the changes a listing has been through, and a listing nobody has edited
            # yet has been through being posted.
            version=1,
            **values.as_column_values(),
        )
        # A listing is born in draft and published, so the state that decides whether it is
        # for sale has been through the same guard as every later move. The move happens
        # before the row is written, because a draft is never what anybody is meant to store.
        transition_listing_status(listing, ListingStatus.ACTIVE)
        await self._listings.add(listing)
        photos = await self._listings.replace_photos(listing.id, list(values.photos))
        # AC-3.1's confirmation, inside the listing's own transaction: the message the seller
        # is owed is written with the listing, so a publish that cannot announce itself has not
        # half-happened. Only the USSD door queues it — the SMS command answers its own POST
        # with `post_success`, and no acceptance criterion asks to text an app seller.
        if source is ListingSource.USSD:
            await self._queue_listing_created(seller_id, listing)
        await self._listings.commit()
        return to_response(
            LoadedListing(listing=listing, photos=photos), available_kg=values.quantity_kg
        )

    async def _queue_listing_created(self, seller_id: UUID, listing: Listing) -> None:
        """The queued row a USSD publish owes its seller, and the words that row carries.

        Queued rather than sent, the same way every other trader-facing message is: the
        drain is what carries it, so a seller whose phone is unreachable has a row waiting
        instead of a confirmation nobody received. The text is rendered here, in the seller's
        own language, because this transaction is the one that already holds the listing it
        describes.

        The dedup key names the listing, not the attempt. A gateway retry of the wizard's
        final screen replays the creation under the same idempotency key and so re-enters
        here with the same listing; a second confirmation for the same listing is not a
        second thing worth saying.
        """
        seller = await self._users.find_by_id(seller_id)
        if seller is None:
            raise ApiError(ErrorCode.INTERNAL_ERROR, details={"reason": "listing_seller_missing"})
        rendered_text, rendered_language = _render_listing_created(
            seller.language, code=listing.short_code
        )
        await self._notifications.add(
            Notification(
                user_id=seller_id,
                channel=NotificationChannel.SMS,
                template_id="listing_created",
                priority=priority_rank(SmsPriority.Q2),
                language=rendered_language,
                rendered_text=rendered_text,
                status=NotificationStatus.QUEUED,
                dedup_key=f"listing_created:{listing.id}",
            )
        )

    async def read(self, listing_id: UUID) -> ListingResponse:
        """A listing as any signed-in caller may see it.

        Availability is asked for and the status is judged against it, so a listing whose stock
        an order has taken to zero reads as sold out whether or not anything has got round to
        writing that down. FR-306's trigger is a fact about the kilos, and a fact about the kilos
        is derived rather than stored.
        """
        loaded = await self._require(listing_id)
        reserved = await self._reservations.reserved_kg_for_listing(loaded.listing.id)
        return to_response(loaded, available_kg=loaded.listing.quantity_kg - reserved)

    async def read_mine(
        self, seller_id: UUID, cursor: str | None, limit: int
    ) -> tuple[list[ListingResponse], str | None, bool]:
        """One page of the caller's own listings, newest first."""
        after = decode_cursor(cursor) if cursor else None
        rows, has_more = await self._listings.page_for_seller(seller_id, after, limit)
        # One statement for the whole page. Availability is read per listing for the status to be
        # judged against it, and asking the database once per listing would make this the slowest
        # read in the API for a seller with twenty postings.
        reserved = await self._reservations.reserved_kg_for_listings(
            [row.listing.id for row in rows[:limit]]
        )
        next_cursor = encode_cursor(rows[-1].listing) if has_more and rows else None
        return (
            [
                to_response(
                    row,
                    available_kg=row.listing.quantity_kg - reserved.get(row.listing.id, 0),
                )
                for row in rows[:limit]
            ],
            next_cursor,
            has_more,
        )

    async def update(
        self, listing_id: UUID, caller_id: UUID, body: dict[str, Any]
    ) -> ListingResponse:
        """Apply the fields that were sent, if the caller still holds the version they read.

        Read under a row lock, because the one edit this service cannot perform is an edit that
        would sell stock somebody has already agreed to buy: FR-1904 puts that enforcement in the
        same transaction that writes the listing, and the sales that hold kilos take the same
        lock. An edit judged against an unlocked read would be judged against a quantity that a
        sale accepted on the other side of the database has already changed.
        """
        loaded = await self._require_owned(listing_id, caller_id, lock=True)
        listing = loaded.listing
        now = datetime.now(UTC)
        expected_version = _required_int(body, "version")

        if expected_version != listing.version:
            # Hand back the row that beat this write, in the same shape as the one the caller
            # tried to change. A caller told only that it lost cannot do anything with the
            # loss; a caller holding the current row can reconcile and try again.
            raise ApiError(
                ErrorCode.CONCURRENT_MODIFICATION,
                details={
                    "listing_id": str(listing.id),
                    "expected_version": expected_version,
                    "current": to_response(
                        loaded, available_kg=await self._available_kg(loaded.listing)
                    ).model_dump(mode="json"),
                },
            )

        status = effective_status(listing, now=now)
        if status in READ_ONLY_STATUSES:
            logger.warning("refused listing edit listing_id=%s status=%s", listing.id, status.value)
            raise ApiError(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                details={
                    "listing_id": str(listing.id),
                    "status": status.value,
                    "read_only": True,
                },
            )

        merged = self._merge(loaded, body)
        validate_listing_values(merged, now=now)
        await self._require_stock_survives_edit(listing, merged.quantity_kg)

        if merged == self._values_of(loaded):
            # Nothing actually changed. The version stays where it is so that two callers who
            # both send the same no-op edit do not invalidate each other forever.
            return to_response(loaded, available_kg=await self._available_kg(listing))

        photos = await self._apply(listing, merged)
        await self._listings.commit()
        return to_response(
            LoadedListing(listing=listing, photos=photos),
            available_kg=merged.quantity_kg
            - await self._reservations.reserved_kg_for_listing(listing.id),
        )

    async def pause(self, listing_id: UUID, caller_id: UUID) -> ListingResponse:
        """Hide a live listing from search without closing it."""
        return await self._move(listing_id, caller_id, ListingStatus.PAUSED)

    async def resume(self, listing_id: UUID, caller_id: UUID) -> ListingResponse:
        """Put a paused listing back in front of buyers."""
        return await self._move(listing_id, caller_id, ListingStatus.ACTIVE)

    async def close(self, listing_id: UUID, caller_id: UUID) -> ListingResponse:
        """End a listing for good, from whatever state it is in.

        The state table allows a close from every live status, so this is the one move that
        is legal from nearly anywhere. Whether an open offer blocks it is the offer slice's
        rule to apply; there is nothing to consult yet, so nothing is pretended.
        """
        return await self._move(listing_id, caller_id, ListingStatus.CLOSED)

    async def _move(
        self, listing_id: UUID, caller_id: UUID, target: ListingStatus
    ) -> ListingResponse:
        loaded = await self._require_owned(listing_id, caller_id)
        transition_listing_status(loaded.listing, target)
        await self._listings.commit()
        return to_response(loaded, available_kg=await self._available_kg(loaded.listing))

    async def _available_kg(self, listing: Listing) -> int:
        """What is left of this listing: its total less every reservation still counting.

        One number with one definition, used by every path that answers with a listing. It is
        asked for rather than kept on the row because FR-1901 puts availability in the
        reservations: a counter that was also maintained would be a second thing to keep true.
        """
        return listing.quantity_kg - await self._reservations.reserved_kg_for_listing(listing.id)

    async def _apply(self, listing: Listing, values: ListingValues) -> list[ListingPhoto]:
        """Write a validated set of values onto the row and move the version on.

        Reached only once the caller has been shown that something really did change, so the
        version moves here without the question being asked twice.
        """
        for field, value in values.as_column_values().items():
            setattr(listing, field, value)
        listing.version += 1
        await self._listings.flush()
        return await self._listings.replace_photos(listing.id, list(values.photos))

    async def _require(self, listing_id: UUID) -> LoadedListing:
        loaded = await self._listings.find_by_id(listing_id)
        if loaded is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "listing"})
        return loaded

    async def _require_owned(
        self, listing_id: UUID, caller_id: UUID, *, lock: bool = False
    ) -> LoadedListing:
        """The listing, and a refusal when it belongs to somebody else.

        Existence is answered before ownership on purpose: a caller who may not touch a row
        still needs to know whether it is there, because the detail view will tell them.

        `lock` for the write paths, because the alternative is judging an edit against a
        quantity that a sale accepted a moment later has already changed.
        """
        loaded = (
            await self._listings.find_by_id_for_update(listing_id)
            if lock
            else await self._listings.find_by_id(listing_id)
        )
        if loaded is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "listing"})
        if loaded.listing.seller_id != caller_id:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                details={"resource": "listing", "reason": "not_the_seller"},
            )
        return loaded

    async def _require_stock_survives_edit(self, listing: Listing, quantity_kg: int) -> None:
        """Refuse a quantity that would put availability below zero, and name what is in the way.

        The invariant is `available = quantity_kg - held - converted - consumed >= 0`, so the
        only question an edit can get wrong is whether the new total is still at least what
        orders have already been given. Raising the quantity is always safe; lowering it is not,
        and the refusal names the orders holding the kilos rather than only reporting a number,
        because a seller who is told "there are 80 kilos committed" still has to work out which
        of four orders that is, and the four are right there.

        Open offers are not consulted, and their not being consulted is the point (AC-3.3): an
        offer reserves nothing, so a seller is free to reduce stock under an unanswered bid, and
        if that leaves the offer too large then its acceptance fails validation later - which is
        the seller refusing it, by arithmetic rather than by a message.
        """
        committed = await self._reservations.reserved_kg_for_listing(listing.id)
        if quantity_kg >= committed:
            return

        blocking = await self._reservations.blocking_for_listing(listing.id)
        logger.warning(
            "refused listing edit listing_id=%s requested_quantity_kg=%s committed_kg=%s",
            listing.id,
            quantity_kg,
            committed,
        )
        raise ApiError(
            ErrorCode.ORDER_CONFLICT,
            details={
                "listing_id": str(listing.id),
                "requested_quantity_kg": quantity_kg,
                "committed_kg": committed,
                "blocking_orders": [
                    {
                        "order_id": str(held.order.id),
                        "short_code": held.order.short_code,
                        "quantity_kg": held.reservation.quantity_kg,
                        "state": held.reservation.state.value,
                        "status": held.order.status.value,
                    }
                    for held in blocking
                ],
            },
        )

    async def _allocate_short_code(self) -> str:
        """A code no other listing holds.

        Drawn from a cryptographic source, because a code is a guessable handle on somebody's
        listing and a predictable one would let one seller read another's by counting. The
        table is checked before the write rather than caught after it, so a collision is an
        ordinary retry and not a failed request.
        """
        for _ in range(SHORT_CODE_ATTEMPTS):
            candidate = "".join(secrets.choice(CROCKFORD_BASE32) for _ in range(SHORT_CODE_LENGTH))
            if not await self._listings.short_code_exists(candidate):
                return candidate
        raise ApiError(  # pragma: no cover - needs a billion codes already taken
            ErrorCode.INTERNAL_ERROR, details={"reason": "short_code_allocation_exhausted"}
        )

    async def _judge_price(self, values: ListingValues, now: datetime) -> bool:
        """Flag this price against the last published average, if there is one to judge."""
        index = await self._price_indices.latest_for(
            values.crop_id, values.region_code, eat_calendar_date(now)
        )
        return is_price_outlier(values.unit_price_santim, _usable_average(index))

    def _values_from(self, body: dict[str, Any], expires_at_default: datetime) -> ListingValues:
        return ListingValues(
            crop_id=_required_int(body, "crop_id"),
            quantity_kg=_required_int(body, "quantity_kg"),
            min_order_kg=_required_int(body, "min_order_kg"),
            unit_price_santim=_required_int(body, "unit_price_santim"),
            harvest_date=_required_date(body, "harvest_date"),
            available_from=_optional_datetime(body, "available_from"),
            expires_at=_optional_datetime(body, "expires_at") or expires_at_default,
            region_code=_required_str(body, "region_code"),
            woreda_id=_required_int(body, "woreda_id"),
            delivery_options=_required_options(body),
            delivery_fee_santim=_optional_int(body, "delivery_fee_santim", default=0),
            description=body.get("description"),
            organic=bool(body.get("organic", False)),
            photos=_photo_triples(body.get("photos") or []),
            listing_attributes=dict(body.get("listing_attributes") or {}),
        )

    def _merge(self, loaded: LoadedListing, body: dict[str, Any]) -> ListingValues:
        """The listing as it would stand with these fields applied over it.

        An absent field is left as it was, which is what an offline client means by sending
        one field. A field sent as null is a request to clear it, and is applied as such;
        that is why the set of what was sent comes from the request and not from the values
        having been defaulted.
        """
        current = self._values_of(loaded)
        updates = {field: value for field, value in body.items() if field != "version"}
        if "photos" in updates:
            updates["photos"] = _photo_triples(updates["photos"])
        if "delivery_options" in updates:
            updates["delivery_options"] = _required_options(updates)
        return replace(current, **updates)

    def _values_of(self, loaded: LoadedListing) -> ListingValues:
        listing = loaded.listing
        return ListingValues(
            crop_id=listing.crop_id,
            quantity_kg=listing.quantity_kg,
            min_order_kg=listing.min_order_kg,
            unit_price_santim=listing.unit_price_santim,
            harvest_date=listing.harvest_date,
            available_from=listing.available_from,
            expires_at=listing.expires_at,
            region_code=listing.region_code,
            woreda_id=listing.woreda_id,
            delivery_options=tuple(listing.delivery_options),
            delivery_fee_santim=listing.delivery_fee_santim,
            description=listing.description,
            organic=listing.organic,
            photos=_photo_triples_of(loaded.photos),
            listing_attributes=dict(listing.listing_attributes),
        )


def validate_listing_values(values: ListingValues, now: datetime) -> None:
    """Hold a listing to its own rules, and say which rules were broken.

    The same checks run for a new listing and for an edit, because a merge that ended in an
    impossible listing would be a worse bug than one that was refused at the door. The
    wording is the seller's field name and the reason, so an app can point at the input
    without parsing prose.
    """
    problems: dict[str, str] = {}

    if not 1 <= values.quantity_kg <= 1_000_000:
        problems["quantity_kg"] = "must_be_1_to_1000000"
    if not 1 <= values.min_order_kg <= values.quantity_kg:
        problems["min_order_kg"] = "must_be_1_to_quantity_kg"
    if values.unit_price_santim <= 0:
        problems["unit_price_santim"] = "must_be_positive"

    today = eat_calendar_date(now)
    if values.harvest_date < today:
        problems["harvest_date"] = "must_not_be_in_the_past"
    elif values.harvest_date > today + MAX_HARVEST_WINDOW:
        problems["harvest_date"] = "must_be_within_365_days"

    if values.expires_at > now + MAX_LIFETIME:
        problems["expires_at"] = "must_be_within_90_days"

    if not values.delivery_options:
        problems["delivery_options"] = "must_not_be_empty"
    # A delivery we carry the cost of has a cost, and a delivery the buyer arranges does not.
    # Stated both ways on purpose: charging for pickup, or delivering free, are both ways of
    # promising something the listing does not say.
    wants_seller_delivery = DeliveryOption.SELLER_DELIVERY in values.delivery_options
    if wants_seller_delivery != (values.delivery_fee_santim > 0):
        problems["delivery_fee_santim"] = (
            "must_be_positive_when_seller_delivery_is_offered_and_zero_otherwise"
        )

    if problems:
        raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": problems})


def to_response(loaded: LoadedListing, available_kg: int | None = None) -> ListingResponse:
    """Read a listing out for a caller, with its status judged against the clock and its stock.

    The photographs come out in their stored order, which is the order the seller chose, and
    the status is the derived one so that a lapsed listing never reads as live.

    `available_kg` is what is left of this listing right now, when the caller has asked. It is
    passed in rather than looked up here because a page of listings needs it for twenty rows at
    once, and twenty separate queries to answer one question is not a trade worth making. Left
    out, the status is judged on the clock alone, which is what the paths that do not care about
    stock need.
    """
    listing = loaded.listing
    status = (
        effective_status(listing)
        if available_kg is None
        else status_for_availability(listing, available_kg)
    )
    return ListingResponse(
        id=listing.id,
        short_code=listing.short_code,
        crop_id=listing.crop_id,
        quantity_kg=listing.quantity_kg,
        sold_kg=listing.sold_kg,
        available_kg=available_kg,
        min_order_kg=listing.min_order_kg,
        unit_price_santim=listing.unit_price_santim,
        harvest_date=listing.harvest_date,
        available_from=listing.available_from,
        expires_at=listing.expires_at,
        region_code=listing.region_code,
        woreda_id=listing.woreda_id,
        delivery_options=list(listing.delivery_options),
        delivery_fee_santim=listing.delivery_fee_santim,
        organic=listing.organic,
        description=listing.description,
        photos=[
            PhotoResponse(
                url=photo.url,
                width=photo.width,
                height=photo.height,
                sort_order=photo.sort_order,
            )
            for photo in sorted(loaded.photos, key=lambda photo: photo.sort_order)
        ],
        status=status,
        price_outlier=listing.price_outlier,
        listing_attributes=dict(listing.listing_attributes),
        version=listing.version,
        created_at=listing.created_at,
        updated_at=listing.updated_at,
    )


def encode_cursor(listing: Listing) -> str:
    """Where a page ended, in a form the caller carries without reading.

    Both the time and the identity are inside it, because ordering by time alone is not an
    order when two listings share a time, which is most of them.
    """
    raw = f"{listing.created_at.isoformat()}|{listing.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Read a page boundary back, refusing one that is not one we wrote."""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        stamp, _, identity = raw.partition("|")
        return datetime.fromisoformat(stamp), UUID(identity)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, details={"fields": {"cursor": "not_a_valid_cursor"}}
        ) from exc


def _default_expiry(now: datetime) -> datetime:
    """When a listing lives if its seller did not say otherwise."""
    return now + DEFAULT_LIFETIME


def _with_sms_defaults(body: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Fill the fields §14.4 says a text-message listing leaves unsaid.

    A POST command carries a crop, a weight and a price and nothing else, and §14.4 says what
    the rest of the listing is then: available now, no minimum order, pickup only. The HTTP
    body already names every one of these fields — ``model_dump`` without ``exclude_unset``
    sends them all — so these defaults fire on the SMS path and never overrule an app that
    meant something else. A copy is taken rather than the caller's dict mutated, because a
    body that arrived from a request is the request's to keep.
    """
    filled = {**body}
    filled.setdefault("min_order_kg", 1)
    filled.setdefault("harvest_date", eat_calendar_date(now))
    filled.setdefault("available_from", now)
    filled.setdefault("delivery_options", [DeliveryOption.PICKUP.value])
    return filled


def _has_lapsed(listing: Listing, now: datetime) -> bool:
    return listing.expires_at <= now


def _usable_average(index: PriceIndex | None) -> int | None:
    """The average a price may be judged against, or nothing at all.

    No row, a row that says it had too few sales, and a row with no average are three
    different shapes of the same answer: we cannot judge this price.
    """
    if index is None or index.insufficient_data or index.avg_unit_price_santim is None:
        return None
    return index.avg_unit_price_santim


def _photo_triples(photos: Any) -> tuple[tuple[str, int, int], ...]:
    """Photographs as the plain tuples the repository writes."""
    return tuple((str(photo["url"]), int(photo["width"]), int(photo["height"])) for photo in photos)


def _photo_triples_of(photos: list[ListingPhoto]) -> tuple[tuple[str, int, int], ...]:
    return tuple((photo.url, photo.width, photo.height) for photo in photos)


def _required_int(body: dict[str, Any], field: str) -> int:
    return int(body[field])


def _optional_int(body: dict[str, Any], field: str, default: int) -> int:
    value = body.get(field)
    return default if value is None else int(value)


def _required_str(body: dict[str, Any], field: str) -> str:
    return str(body[field])


def _required_date(body: dict[str, Any], field: str) -> date:
    value = body[field]
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _optional_datetime(body: dict[str, Any], field: str) -> datetime | None:
    value = body.get(field)
    if value is None:
        return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _required_options(body: dict[str, Any]) -> tuple[DeliveryOption, ...]:
    return tuple(DeliveryOption(option) for option in body["delivery_options"])
