"""The rules for a buyer bidding on a listing, and for the two parties answering each other.

An offer is the cheapest promise on the platform. It reserves nothing, moves no money, and can
be walked away from by either side at any time, which is exactly why the rules here are about
*fairness of access* rather than about protecting a commitment: nobody may flood a listing with
bids that hold nothing, nobody may propose terms the listing's own floor forbids, and nobody may
drag a negotiation past the point where the other side still wants it.

The status is not a field a request may set. There is exactly one function in this module that
writes an offer's status, it consults the state table before it does, and everything that
reaches a status goes through it — including creation, which is the entry into the table's one
non-terminal state rather than a field the client names. That is why an endpoint cannot drift
into permitting something the table forbids: there is nowhere else for a status to be written.

Expiry is read against the clock rather than waited for, on the same pattern as a listing: an
open offer past its `valid_until` reads as expired and is treated as expired by anything that
tries to move it, without any job having to run first. That includes the cap below, which is
counted against the clock rather than off the stored status — a lapsed bid must stop occupying
a slot, or a listing whose buyers had all timed out would refuse new bids for no reason a
caller could see.

Who is speaking in a thread is derived from the depth, never stored. An original is the buyer's
(because a thread begins with a buyer), and each counter answers the other side, so even depths
are the buyer's terms and odd depths are the seller's. That single derivation is what makes the
counter authorization check answerable at all: the party who may answer an offer is the one who
did not write it, and "who wrote it" is a function of the row rather than a column that could
disagree with the row.

Acceptance is not here, deliberately. `OPEN → ACCEPTED` is in the state table because the
specification's table is the whole table and a guard missing an edge is a guard nobody can
trust, but the move itself creates an order atomically and needs the row locking and the
reservation arithmetic of FR-505, which is the orders slice's work. An endpoint that accepted an
offer without those would be a lie told over HTTP.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from apps.api.core.idempotency import IdempotencyStore, once
from apps.api.core.idempotency import fingerprint_of as _fingerprint
from apps.api.models.crop import Crop
from apps.api.models.listing import Listing, ListingStatus
from apps.api.models.notification import Notification, NotificationChannel, NotificationStatus
from apps.api.models.offer import (
    MAX_THREAD_DEPTH,
    Offer,
    OfferStatus,
    Proposer,
    proposing_party,
)
from apps.api.models.order import PaymentMode
from apps.api.repositories.crop_repository import CropRepository
from apps.api.repositories.listing_repository import ListingRepository, LoadedListing
from apps.api.repositories.notification_repository import NotificationRepository
from apps.api.repositories.offer_repository import OfferInContext, OfferRepository
from apps.api.repositories.user_repository import UserRepository
from apps.api.schemas.offer import (
    DEFAULT_VALID_HOURS,
    MAX_VALID_HOURS,
    MIN_VALID_HOURS,
    OfferResponse,
)
from apps.api.services.listing_service import effective_status as effective_listing_status
from packages.shared.errors import ApiError, ErrorCode
from packages.shared.i18n.languages import Language
from packages.shared.i18n.templates import render_template
from packages.shared.messaging.dispatcher import format_etb
from packages.shared.messaging.queues import SmsPriority, priority_rank

logger = logging.getLogger("gebeyalink.api")

#: How many open offers one listing may carry. An anti-spam and fairness cap rather than an
#: inventory rule: an open offer holds nothing, so ten buyers bidding for the same hundred kilos
#: are all bidding honestly, and the eleventh is noise that would bury the ten.
MAX_OPEN_OFFERS_PER_LISTING = 10

#: The most a bid may exceed the asking price. Twice the ask is generous enough to be a real
#: negotiation and bounded enough that a bid is not a blank cheque; the floor is a single santim
#: because "any positive price" is what the specification says, and a buyer offering nothing for
#: something is expressing something else, which is not this endpoint's job to interpret.
MAX_PRICE_MULTIPLE = 2

#: The whole state table, as which statuses may follow which. Absent from a row means the status
#: is terminal: an accepted, refused, withdrawn or lapsed offer is a record of how the
#: negotiation ended, and none of those is renegotiated in place. A counter always writes a new
#: row and supersedes the old one, which is why SUPERSEDED leaves from OPEN like the others.
ALLOWED_TRANSITIONS: dict[OfferStatus, frozenset[OfferStatus]] = {
    OfferStatus.OPEN: frozenset(
        {
            OfferStatus.ACCEPTED,
            OfferStatus.REJECTED,
            OfferStatus.WITHDRAWN,
            OfferStatus.EXPIRED,
            OfferStatus.SUPERSEDED,
        }
    ),
    OfferStatus.ACCEPTED: frozenset(),
    OfferStatus.REJECTED: frozenset(),
    OfferStatus.WITHDRAWN: frozenset(),
    OfferStatus.EXPIRED: frozenset(),
    OfferStatus.SUPERSEDED: frozenset(),
}

#: The one status a row may be created in. Kept apart from the table above only because
#: creation has no state to come from, and saying so is more honest than pretending an offer is
#: born somewhere and moves.
INITIAL_STATUSES = frozenset({OfferStatus.OPEN})


@dataclass(frozen=True, slots=True)
class OfferTerms:
    """Every field an offer's shape is judged by, at one moment.

    Both an opening bid and a counter resolve to one of these before anything is written, so a
    seller's answer is held to exactly the rules a buyer's opening bid is.

    The window is carried as the hours the buyer asked for rather than as the instant it
    becomes. The two are the same statement, but only one of them can be checked against a
    bound: turning a moment back into hours means dividing by 3600 and rounding, and a request
    for exactly the minimum would come back as 23.
    """

    quantity_kg: int
    price_santim: int
    valid_hours: int
    payment_mode: PaymentMode

    def valid_until_from(self, now: datetime) -> datetime:
        """The instant these terms stop being on the table."""
        return now + timedelta(hours=self.valid_hours)

    def as_column_values(self, now: datetime) -> dict[str, Any]:
        """The subset the offers table itself holds."""
        return {
            "quantity_kg": self.quantity_kg,
            "price_santim": self.price_santim,
            "valid_until": self.valid_until_from(now),
            "payment_mode": self.payment_mode,
        }


def transition_offer_status(
    offer: Offer,
    target: OfferStatus,
    from_status: OfferStatus | None,
    *,
    now: datetime | None = None,
) -> OfferStatus:
    """Move an offer to a new status, if the state table allows it.

    This is the only function in the platform that writes an offer's status, and it is called
    by everything that moves one: placing, countering, rejecting, withdrawing, and expiry. A
    caller that has not consulted it has not moved an offer.

    `from_status` is passed in rather than read off the row, so that the callers cannot disagree
    about what the offer's current state is. A caller holding a stored row passes
    `effective_status(offer, now)` — the status judged against the clock — and creation passes
    `None` to mean "not stored yet": the one move with no state behind it, allowed only to land
    on a status a new offer may have.

    Expiry is folded into `effective_status` rather than left to each caller, because the rule
    that a lapsed offer has already expired belongs to the status and not to whichever operation
    happened to look at it. Without it an expired offer would still be rejectable, and two
    readers could disagree about whether a negotiation was still live.
    """
    if from_status is None:
        allowed = INITIAL_STATUSES
        reported: str | None = None
    else:
        allowed = ALLOWED_TRANSITIONS[from_status]
        reported = from_status.value

    if target not in allowed:
        logger.warning(
            "refused offer transition offer_id=%s from=%s to=%s",
            offer.id,
            reported if reported is not None else "new",
            target.value,
        )
        raise ApiError(
            ErrorCode.ILLEGAL_STATE_TRANSITION,
            details={
                "offer_id": str(offer.id),
                "status": reported,
                "requested": target.value,
            },
        )
    offer.status = target
    return target


def effective_status(offer: Offer, now: datetime | None = None) -> OfferStatus:
    """The status an offer has when the clock is consulted, rather than when it was written.

    An open offer past its window is expired, whether or not anything has got round to writing
    that down. Deriving it rather than storing it is what stops a bid nobody answered from
    standing open forever, and it means a read never has to write in order to answer honestly.
    """
    moment = now or datetime.now(UTC)
    if offer.status is OfferStatus.OPEN and offer.valid_until <= moment:
        return OfferStatus.EXPIRED
    return offer.status


def may_answer(offer: Offer, listing: Listing, caller_id: UUID) -> bool:
    """Whether this caller is the party entitled to answer these terms.

    The party who wrote an offer may not answer it. The depth says who wrote it — even is the
    buyer, odd is the seller — so the party who may answer is always the other one, and it is
    always either the listing's seller or the buyer who opened the thread. Answering one's own
    terms would be a way to move an offer without the counterparty, which is the one thing a
    negotiation cannot mean.
    """
    answering = (
        listing.seller_id
        if proposing_party(offer.thread_depth) is Proposer.BUYER
        else offer.buyer_id
    )
    return caller_id == answering


def refuse_disabled_mode(mode: PaymentMode, *, escrow_enabled: bool, cod_enabled: bool) -> None:
    """Refuse a payment mode the platform has not switched on, naming it.

    A mode that is off is a request for something that does not exist, which is a 422 answered by
    name rather than by field: a client cannot fix it by correcting an input, only by asking for a
    mode that is on. Escrow and COD are checked the same way so the two read alike, and the check
    lives here rather than on each route because a mode is refused wherever it can be chosen —
    placing an offer, accepting one, and buying at the asking price.
    """
    if mode is PaymentMode.ESCROW and not escrow_enabled:
        raise ApiError(ErrorCode.VALIDATION_ERROR, details={"feature": "escrow"})
    if mode is PaymentMode.COD and not cod_enabled:
        raise ApiError(ErrorCode.VALIDATION_ERROR, details={"feature": "cod"})


#: The crop column to read for each language, so a message names the crop in the words its
#: reader uses. Oromo and Tigrinya are nullable, which is why the lookup below falls back.
_NAME_COLUMNS: dict[Language, str] = {
    Language.AM: "name_am",
    Language.OM: "name_om",
    Language.EN: "name_en",
}


def _crop_name(crop: Crop, language: Language) -> str:
    """The crop's name in the reader's language, or the English one it does not carry.

    A catalogue that holds no word for the crop in this language falls back the way every
    message falls back, so the seller reads a name rather than a blank. English is the floor
    because it is the one column that is never null.
    """
    column = _NAME_COLUMNS.get(language, "name_en")
    return getattr(crop, column, None) or crop.name_en


def _render_offer_event(template_id: str, language: Language, **fields: str) -> tuple[str, str]:
    """The words one offer event owes, and the language they were actually written in.

    Rendered from the registry rather than from a sentence built here, so the template is the
    one place the message lives and the producer cannot drift from it. The second value is the
    language of the words returned rather than the one asked for, because the row stores the
    language it holds and those part company the moment the fallback fires.
    """
    return render_template(template_id, language, **fields)


def _render_offer_received(language: Language, *, crop: str, price: str) -> tuple[str, str]:
    """The message the seller reads, and the language its words were actually written in.

    Kept as its own name because the placement path read it by that name before the other
    offer events existed; it is now one call to the registry's renderer like the rest.
    """
    return _render_offer_event("offer_received", language, crop=crop, price=price)


class OfferService:
    def __init__(
        self,
        offers: OfferRepository,
        listings: ListingRepository,
        notifications: NotificationRepository,
        users: UserRepository,
        crops: CropRepository,
        idempotency: IdempotencyStore,
        *,
        escrow_enabled: bool,
        cod_enabled: bool,
    ) -> None:
        self._offers = offers
        self._listings = listings
        self._notifications = notifications
        self._users = users
        self._crops = crops
        self._idempotency = idempotency
        self._escrow_enabled = escrow_enabled
        self._cod_enabled = cod_enabled

    async def place(
        self, listing_id: UUID, buyer_id: UUID, body: dict[str, Any], key: str | None = None
    ) -> OfferResponse:
        """Put a buyer's terms on a live listing.

        Bounded by the listing on three axes at once — its floor, its total quantity, and twice
        its asking price — because an offer that ignored any of them would be a second route to
        posting a listing on terms the listing service would have refused.

        ``key`` is the SMS channel's idempotency key and is absent on the app path; when it is
        present the whole placement runs under it, so a gateway retry places one bid.
        """
        if key is None:
            return await self._place(listing_id, buyer_id, body)
        return await once(
            self._idempotency,
            account_id=str(buyer_id),
            key=key,
            fingerprint=_fingerprint("offer", str(listing_id), body),
            work=lambda: self._place(listing_id, buyer_id, body),
            encode=lambda response: (201, response.model_dump(mode="json")),
            decode=OfferResponse.model_validate,
        )

    async def _place(self, listing_id: UUID, buyer_id: UUID, body: dict[str, Any]) -> OfferResponse:
        now = datetime.now(UTC)
        listing = await self._require_live_listing(listing_id, now)

        # A seller bidding on their own listing is not negotiating with anybody, and letting it
        # through would put a row in that seller's own offer list which every rule below then
        # reads as somebody's bid. There is no version of this request worth serving.
        if listing.seller_id == buyer_id:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={"fields": {"listing_id": "you_cannot_offer_on_your_own_listing"}},
            )

        terms = terms_from(body)
        # The mode is chosen here, when the thread opens, so this is where it is held to the
        # flags. A counter never re-chooses it; see `counter`.
        refuse_disabled_mode(
            terms.payment_mode,
            escrow_enabled=self._escrow_enabled,
            cod_enabled=self._cod_enabled,
        )
        validate_offer_terms(terms, listing)

        # Counted against the clock, not off the stored status: a lapsed bid must not go on
        # occupying one of the ten slots (see `count_still_open`).
        already_open = await self._offers.count_still_open(listing_id, now)
        if already_open >= MAX_OPEN_OFFERS_PER_LISTING:
            logger.warning("refused offer listing_id=%s open_offers=%s", listing_id, already_open)
            raise ApiError(
                ErrorCode.OFFER_LIMIT,
                details={
                    "listing_id": str(listing_id),
                    "limit": MAX_OPEN_OFFERS_PER_LISTING,
                    "open_offers": already_open,
                },
            )

        offer = Offer(
            listing_id=listing_id,
            buyer_id=buyer_id,
            # An original opens its own thread and writes the id it will later be found by, so
            # that every row in the negotiation answers the same question.
            thread_id=uuid.uuid4(),
            thread_depth=0,
            status=OfferStatus.OPEN,
            # The first version of a row is one, as on a listing: version counts the changes a
            # row has been through, and an offer nobody has answered yet has been through being
            # placed. Nothing edits an offer in place; a change of terms is a new row.
            version=1,
            **terms.as_column_values(now),
        )
        transition_offer_status(offer, OfferStatus.OPEN, None)
        await self._offers.add(offer)
        await self._queue_offer_received(offer, listing)
        await self._offers.commit()
        return to_response(offer, now=now)

    async def _queue_offer_received(self, offer: Offer, listing: Listing) -> None:
        """Write the seller's message into the queue, inside the offer's own transaction.

        The seller is told by a row rather than by a send: this writes the message the drain
        will carry, on the same session as the offer, so the two are one commit — a message
        that cannot be written is not an offer that stands announced to nobody. The reads are
        trusted rather than guarded: the listing's foreign keys guarantee the seller and the
        crop exist, so a read that finds neither is a broken database, and the placement fails
        loudly rather than skipping the message a corrupt read refused to describe.

        The language is the seller's and the text is rendered in it; the row records the
        language of the words it holds, which is the seller's unless the template carries no
        variant for it, in which case it carries the English fallback's.
        """
        seller = await self._users.find_by_id(listing.seller_id)
        if seller is None:
            raise ApiError(ErrorCode.INTERNAL_ERROR, details={"reason": "offer_seller_missing"})
        crop = await self._crops.find_by_id(listing.crop_id)
        if crop is None:
            raise ApiError(ErrorCode.INTERNAL_ERROR, details={"reason": "offer_crop_missing"})

        rendered_text, rendered_language = _render_offer_received(
            seller.language,
            crop=_crop_name(crop, seller.language),
            price=format_etb(offer.price_santim),
        )
        await self._notifications.add(
            Notification(
                user_id=listing.seller_id,
                channel=NotificationChannel.SMS,
                template_id="offer_received",
                priority=priority_rank(SmsPriority.Q2),
                language=rendered_language,
                rendered_text=rendered_text,
                status=NotificationStatus.QUEUED,
                dedup_key=f"offer_received:{offer.id}",
            )
        )

    async def _queue_offer_rejected(self, offer: Offer, listing: Listing) -> None:
        """Write the buyer's message into the queue, inside the refusal's own transaction.

        A refusal is the seller's word on the buyer's bid, so the buyer is the one owed the
        sentence that ends the wait — a bid that simply stopped showing in a list would leave
        its owner checking forever. The row is written on the same session as the refusal and
        the two are one commit, the N1b pattern the accepted and lifecycle messages follow too.

        The reads are trusted rather than guarded, for `_queue_offer_received`'s reason: the
        listing's foreign keys guarantee the buyer and the crop exist, so a read that finds
        neither is a broken database and the refusal fails loudly rather than closing a bid
        with no word to the party who was waiting on it.
        """
        buyer = await self._users.find_by_id(offer.buyer_id)
        if buyer is None:
            raise ApiError(ErrorCode.INTERNAL_ERROR, details={"reason": "offer_buyer_missing"})
        crop = await self._crops.find_by_id(listing.crop_id)
        if crop is None:
            raise ApiError(ErrorCode.INTERNAL_ERROR, details={"reason": "offer_crop_missing"})

        rendered_text, rendered_language = _render_offer_event(
            "offer_rejected",
            buyer.language,
            crop=_crop_name(crop, buyer.language),
        )
        await self._notifications.add(
            Notification(
                user_id=offer.buyer_id,
                channel=NotificationChannel.SMS,
                template_id="offer_rejected",
                priority=priority_rank(SmsPriority.Q2),
                language=rendered_language,
                rendered_text=rendered_text,
                status=NotificationStatus.QUEUED,
                dedup_key=f"offer_rejected:{offer.id}",
            )
        )

    async def read_for_listing(
        self, listing_id: UUID, caller_id: UUID, now: datetime | None = None
    ) -> list[OfferResponse]:
        """Every offer on a listing, for the seller who owns it and nobody else.

        Seller-only, and not for privacy's sake: a buyer's bid is a price they are willing to
        pay, so letting one buyer read the others would turn this list into a floor for the next
        bid. A buyer learns what they themselves offered, from the offer they hold.

        Not paginated here. The volume that matters is capped — ten open offers at a time — and
        what the seller needs from this list is the whole thread, because a negotiation read in
        pieces is a negotiation whose earlier terms are missing. If a listing's history ever
        grows past what a phone should render, this is the endpoint that will need a cursor, and
        it will need to keep returning a thread whole.
        """
        moment = now or datetime.now(UTC)
        listing = (await self._require_listing(listing_id)).listing
        if listing.seller_id != caller_id:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                details={"resource": "listing", "reason": "not_the_seller"},
            )
        rows = await self._offers.list_for_listing(listing_id)
        return [to_response(offer, now=moment) for offer in rows]

    async def reject(
        self, offer_id: UUID, caller_id: UUID, key: str | None = None
    ) -> OfferResponse:
        """The seller's answer that ends the negotiation: no.

        The seller's alone, at any depth, including against the seller's own counter. A refusal
        is the listing owner's word on a bid for that listing, which is a fact about the listing
        rather than about whose turn it is.

        Legal from an open offer whatever state the listing is in, including one that has since
        been paused or closed. Refusing an offer proposes nothing on the listing and reserves
        nothing, so a seller must never be unable to close out a bid just because the listing
        behind it moved — that would leave a buyer's offer open on a listing nobody is selling.

        ``key`` is the SMS channel's idempotency key and is absent on the app path.
        """
        if key is None:
            return await self._reject(offer_id, caller_id)
        return await once(
            self._idempotency,
            account_id=str(caller_id),
            key=key,
            fingerprint=_fingerprint("reject", str(offer_id)),
            work=lambda: self._reject(offer_id, caller_id),
            encode=lambda response: (200, response.model_dump(mode="json")),
            decode=OfferResponse.model_validate,
        )

    async def _reject(self, offer_id: UUID, caller_id: UUID) -> OfferResponse:
        context = await self._require_rejectable(offer_id, caller_id)
        now = datetime.now(UTC)
        transition_offer_status(
            context.offer, OfferStatus.REJECTED, effective_status(context.offer, now), now=now
        )
        await self._queue_offer_rejected(context.offer, context.listing)
        await self._offers.commit()
        return to_response(context.offer, now=now)

    async def counter(self, offer_id: UUID, caller_id: UUID, body: dict[str, Any]) -> OfferResponse:
        """Answer an open offer with different terms, in the same thread.

        Writes two rows: a new offer one level deeper, and the offer it answers marked
        superseded. Nothing is edited in place, so the negotiation stays readable afterwards —
        what was offered, what was asked back, and in what order.
        """
        context = await self._require_answerable(offer_id, caller_id)
        offer = context.offer
        now = datetime.now(UTC)

        # A counter is a new offer on this listing, so the listing has to be one that can still
        # receive offers. Refusing and withdrawing are exempt, for the reason given on reject.
        await self._require_live_listing(context.listing.id, now)

        if offer.thread_depth >= MAX_THREAD_DEPTH:
            logger.warning(
                "refused counter offer_id=%s thread_depth=%s", offer.id, offer.thread_depth
            )
            raise ApiError(
                ErrorCode.LIMIT_EXCEEDED,
                details={
                    "offer_id": str(offer.id),
                    "thread_id": str(offer.thread_id),
                    "thread_depth": offer.thread_depth,
                    "limit": MAX_THREAD_DEPTH,
                    "max_counters": MAX_THREAD_DEPTH,
                },
            )

        # The mode is inherited, not chosen: a counter answers terms on the same ground they were
        # offered on. A body carrying a mode never reaches here — the counter body forbids it —
        # so the offer being answered is the only source, and neither party can change it by
        # countering.
        terms = terms_from(body, payment_mode=offer.payment_mode)
        validate_offer_terms(terms, context.listing)

        counter_offer = Offer(
            listing_id=offer.listing_id,
            # The buyer does not change along a thread: this column names who the negotiation
            # is with, and a counter by the seller is still a counter on the buyer's
            # negotiation. Which side is speaking is the depth's job.
            buyer_id=offer.buyer_id,
            thread_id=offer.thread_id,
            thread_depth=offer.thread_depth + 1,
            status=OfferStatus.OPEN,
            version=1,
            **terms.as_column_values(now),
        )
        transition_offer_status(counter_offer, OfferStatus.OPEN, None)
        await self._offers.add(counter_offer)

        # The offer being answered stops being on the table, and it does so through the same
        # guard as every other move. The new row is written first so that a refusal here cannot
        # leave a negotiation with nothing on the table and no answer given.
        transition_offer_status(
            offer, OfferStatus.SUPERSEDED, effective_status(offer, now), now=now
        )
        await self._offers.commit()
        return to_response(counter_offer, now=now)

    async def withdraw(self, offer_id: UUID, caller_id: UUID) -> OfferResponse:
        """The buyer taking their own terms back off the table.

        The buyer's alone, at any depth. A seller who wants an offer to stop has `reject`,
        which says something quite different to the buyer: a refusal is an answer, and a
        withdrawal is the absence of one. One word for both would leave the seller's "no"
        looking like the buyer changed their mind.
        """
        context = await self._require_withdrawable(offer_id, caller_id)
        now = datetime.now(UTC)
        transition_offer_status(
            context.offer, OfferStatus.WITHDRAWN, effective_status(context.offer, now), now=now
        )
        await self._offers.commit()
        return to_response(context.offer, now=now)

    async def _require_listing(self, listing_id: UUID) -> LoadedListing:
        loaded = await self._listings.find_by_id(listing_id)
        if loaded is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "listing"})
        return loaded

    async def _require_live_listing(self, listing_id: UUID, now: datetime) -> Listing:
        """The listing, and a refusal when it is not currently on sale.

        Read through the listing service's own rule for what a listing's status is, so a lapsed
        listing is as unavailable to a new bid here as it is to a search: one definition of
        "live", not two that can drift.
        """
        loaded = await self._require_listing(listing_id)
        status = effective_listing_status(loaded.listing, now=now)
        if status is not ListingStatus.ACTIVE:
            logger.warning("refused offer on listing_id=%s status=%s", listing_id, status.value)
            raise ApiError(
                ErrorCode.LISTING_NOT_ACTIVE,
                details={"listing_id": str(listing_id), "status": status.value},
            )
        return loaded.listing

    async def _require_offer(self, offer_id: UUID) -> OfferInContext:
        context = await self._offers.find_in_context(offer_id)
        if context is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "offer"})
        return context

    async def _require_answerable(self, offer_id: UUID, caller_id: UUID) -> OfferInContext:
        """The offer, and a refusal when this caller is not the party entitled to answer it.

        Existence is answered before authorization on purpose: a caller who may not touch a row
        still needs to know whether it is there, because the listing detail view will tell them.
        """
        context = await self._require_offer(offer_id)
        if not may_answer(context.offer, context.listing, caller_id):
            raise ApiError(
                ErrorCode.FORBIDDEN,
                details={
                    "resource": "offer",
                    "reason": "not_the_answering_party",
                    "proposed_by": proposing_party(context.offer.thread_depth).value,
                },
            )
        return context

    async def _require_rejectable(self, offer_id: UUID, caller_id: UUID) -> OfferInContext:
        """The offer, and a refusal when this caller is not the seller who owns the listing.

        A refusal is the seller's alone, and it is held to that rather than to whoever the
        depth says may answer. Parity decides whose turn it is; it does not decide who owns the
        listing. A seller who has countered is speaking on their own terms at an odd depth, and
        parity would hand those terms back to the buyer to answer — so a buyer refusing a
        seller's counter would end the negotiation with the seller's "no" recorded as the
        buyer's. The answerable check is what `counter` uses; this one is deliberately a
        different question, asked of the listing's seller rather than of the depth.
        """
        context = await self._require_offer(offer_id)
        if context.listing.seller_id != caller_id:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                details={"resource": "offer", "reason": "not_the_seller"},
            )
        return context

    async def _require_withdrawable(self, offer_id: UUID, caller_id: UUID) -> OfferInContext:
        """The offer, and a refusal when this caller is not the buyer who placed it.

        SEC-05: the buyer named on the row is the only party who may withdraw it. The seller
        rejecting and the buyer withdrawing are different acts by different people, and an
        endpoint that let the seller withdraw would let them end a negotiation without the
        buyer ever being told.
        """
        context = await self._require_offer(offer_id)
        if context.offer.buyer_id != caller_id:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                details={"resource": "offer", "reason": "not_the_buyer"},
            )
        return context


def terms_from(body: dict[str, Any], *, payment_mode: PaymentMode | None = None) -> OfferTerms:
    """The terms as stated, with the default window filled in.

    `payment_mode` is passed in by a counter, which inherits the mode of the offer it answers
    rather than reading one from its own body. An opening bid leaves it out and takes the body's
    value instead, defaulting to escrow so a client written before the field existed keeps
    working.
    """
    hours = body.get("valid_hours")
    return OfferTerms(
        quantity_kg=int(body["quantity_kg"]),
        price_santim=int(body["price_santim"]),
        valid_hours=DEFAULT_VALID_HOURS if hours is None else int(hours),
        payment_mode=(
            payment_mode
            if payment_mode is not None
            else PaymentMode(body.get("payment_mode", PaymentMode.ESCROW.value))
        ),
    )


def validate_offer_terms(terms: OfferTerms, listing: Listing) -> None:
    """Hold an offer to the listing's own terms, and say which rule was broken.

    The same checks run for an opening bid and for a counter, because an answer that could
    propose what a bid could not would make the floor meaningless. The wording is the field's
    name and the reason, so an app can point at the input without parsing prose.

    The quantity is checked against the listing's *total*, not against what other offers would
    leave. An open offer reserves nothing (FR-1903), so ten honest bids for the same hundred
    kilos are all placeable, and refusing the tenth for the ninth's sake would be refusing
    arithmetic rather than a rule.
    """
    problems: dict[str, str] = {}

    ceiling = listing.unit_price_santim * MAX_PRICE_MULTIPLE
    if not 1 <= terms.price_santim <= ceiling:
        problems["price_santim"] = f"must_be_1_to_{MAX_PRICE_MULTIPLE}x_unit_price_santim"

    if not listing.min_order_kg <= terms.quantity_kg <= listing.quantity_kg:
        problems["quantity_kg"] = "must_be_between_min_order_kg_and_listing_quantity_kg"

    if not MIN_VALID_HOURS <= terms.valid_hours <= MAX_VALID_HOURS:
        problems["valid_hours"] = f"must_be_{MIN_VALID_HOURS}_to_{MAX_VALID_HOURS}"

    if problems:
        raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": problems})


def to_response(offer: Offer, *, now: datetime | None = None) -> OfferResponse:
    """Read an offer out for a caller, with its status judged against the clock.

    The status is the derived one so a lapsed offer never reads as a standing bid, and the
    proposer is derived from the depth for the same reason: a client deciding what it may do
    next needs to know whose turn it is, and that is a function of the row rather than a column
    that could disagree with it.
    """
    moment = now or datetime.now(UTC)
    return OfferResponse(
        id=offer.id,
        listing_id=offer.listing_id,
        buyer_id=offer.buyer_id,
        thread_id=offer.thread_id,
        thread_depth=offer.thread_depth,
        quantity_kg=offer.quantity_kg,
        price_santim=offer.price_santim,
        valid_until=offer.valid_until,
        status=effective_status(offer, moment),
        proposed_by=proposing_party(offer.thread_depth),
        version=offer.version,
        created_at=offer.created_at,
        updated_at=offer.updated_at,
    )
