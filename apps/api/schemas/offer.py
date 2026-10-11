"""Request and response bodies for offers.

An offer states terms and nothing else. It does not state who it is for, what state it starts
in, which thread it belongs to or how deep in that thread it sits — the seller is whoever holds
the token, the state is reached through the transition guard rather than chosen, and the thread
and depth follow from whether this is an opening bid or an answer to one. A client that could
set those would be able to write itself into the middle of somebody else's negotiation, so
there are no fields for them here at all rather than fields that are quietly ignored.

`valid_hours` is the one field a client sends that the platform then converts to a moment:
`valid_until` is computed as now plus these hours, and stored. The instant is stored rather
than recomputed from the hours on every read, because "how long did they have" is a question
with one answer — the one that was true when the offer was made — and re-deriving it would
quietly change an offer's deadline every time it was read.

The response carries `proposed_by` because the depth alone means nothing to a client that has to
decide whose turn it is. It is derived from the depth on the way out rather than read from a
column, for the same reason it is not stored on the way in.

How a sale would be paid for is chosen once, on the opening bid, and carried by every counter in
the thread. It is the one term a negotiation does not renegotiate: a client that could change it
by answering could move the ground under the other party after they had agreed to the price and
the weight, so the field belongs to the opening bid and the counter body refuses to carry one at
all.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from apps.api.models.offer import OfferStatus, Proposer
from apps.api.models.order import PaymentMode

#: How long an offer lives if the buyer does not say otherwise. Seventy-two hours is the
#: specification's default: long enough for a trader to hear back, short enough that a stale
#: bid does not sit in front of the seller for a week.
DEFAULT_VALID_HOURS = 72

#: The window a buyer may choose, in hours. Below a day a seller cannot realistically answer,
#: and above a week the crop may no longer be what the listing described.
MIN_VALID_HOURS = 24
MAX_VALID_HOURS = 168


class OfferTermsBody(BaseModel):
    """The terms a bid states, shared by an opening bid and a counter.

    Held apart from either body so that the two carry exactly the fields they own: weight, price
    and window are negotiable in both, while the payment mode is chosen only when a thread opens
    and is inherited by the counters that answer it.
    """

    model_config = ConfigDict(extra="forbid")

    quantity_kg: int = Field(ge=1, le=1_000_000)
    price_santim: int = Field(gt=0)
    valid_hours: int = Field(default=DEFAULT_VALID_HOURS, ge=MIN_VALID_HOURS, le=MAX_VALID_HOURS)


class OfferCreateBody(OfferTermsBody):
    """The terms a buyer is bidding, and the mode they are bidding on.

    Only what is relative to the listing is left out: the ceiling of twice the asking price and
    the weight floor of the listing's minimum order are both rules about a listing this request
    does not carry, so they are checked where the listing is read.
    """

    payment_mode: PaymentMode = PaymentMode.ESCROW


class OfferCounterBody(OfferTermsBody):
    """A seller's answer to an open offer, on different or the same terms.

    The same terms as an opening bid on purpose. A counter is a new offer with new terms, and
    holding it to the same bounds as an opening bid is what stops an answer from being a way to
    propose something an opening bid could not have proposed. What it deliberately does *not*
    carry is a payment mode: a counter keeps the mode of the offer it answers, so how a sale
    would be paid for is chosen when the thread opens and cannot be changed by answering. A body
    that tried anyway is refused by the inherited `extra="forbid"`.
    """


class OfferResponse(BaseModel):
    """An offer as it is read back.

    `status` is the derived one, so an offer whose window has closed reads as expired rather
    than as a standing bid nobody has answered. `proposed_by` is derived from the depth for the
    same reason: a client deciding what it may do next needs to know whose turn it is.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    listing_id: UUID
    buyer_id: UUID
    thread_id: UUID
    thread_depth: int = Field(ge=0)
    quantity_kg: int
    price_santim: int
    valid_until: datetime
    status: OfferStatus
    proposed_by: Proposer
    version: int
    created_at: datetime
    updated_at: datetime
