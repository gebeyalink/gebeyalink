"""A buyer's bid on a listing, and the thread a negotiation grows into.

An offer is not an order and it does not become one by itself. It is a statement of terms that
somebody else may accept, refuse, or answer with different terms, and until one of those three
things happens it holds nothing: no crop is reserved against it and no money moves. That is why
`quantity_kg` is checked against the listing's *total* quantity rather than against what is
left after other bids — ten open offers on the same hundred kilos are all honestly placeable,
and which of them turns into an order is decided later, by whoever accepts first.

Two fields here are less obvious than they look.

`thread_id` groups the whole negotiation, and it is written on every row including the first,
where it is a value the row invents for itself rather than one it inherits. There is no parent
pointer, so a thread is found by asking for every row carrying the same id. A nullable
`thread_id` would have been the alternative — an original with none and counters with one — and
it would have made "which thread is this in" a question with two answers depending on which row
you asked. Every offer is in exactly one thread, so the column is not nullable and the original
generates the value it will later be found by.

`thread_depth` is how far down that thread this row sits: zero for a buyer's opening bid, one
for the seller's answer, two for the buyer's reply to that, and so on. It is the only record of
whose turn it is, and it is the reason there is no proposer column to drift out of step with it
— the depth *is* the proposer, alternating by parity. A counter that flipped the roles in the
data as well as in the conversation would let the two disagree, and the authorization decision
would then depend on which column a given code path remembered to read.

`payment_mode` is not a negotiable term. It is chosen when a thread opens and written on every
row in the thread, so a counter inherits it rather than restating it, and an accepted offer can
hand the mode to the order it becomes without reading configuration that may since have changed.
A mode a deployment has switched off must not change what an offer that already exists means.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    text,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base, TimestampMixin
from apps.api.db.enums import stored_values
from apps.api.models.order import PaymentMode


class OfferStatus(enum.StrEnum):
    """Where an offer sits in its own short life.

    Only `OPEN` has anywhere to go. Everything else is a record of how the negotiation ended,
    and a record of that kind is not edited, reopened or renegotiated — a superseded bid is not
    a bid anybody may return to, it is what the counter that replaced it was an answer to.
    """

    OPEN = "OPEN"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class Proposer(enum.StrEnum):
    """Which side of the negotiation put these terms on the table.

    Derived from the depth rather than stored, because the depth already says it and a second
    column saying the same thing is a second thing that can be wrong. Even depths are the
    buyer's terms and odd depths are the seller's, starting from the fact that a thread begins
    with a buyer.
    """

    BUYER = "BUYER"
    SELLER = "SELLER"


#: The deepest an offer may sit in a thread. The specification allows an original and three
#: counters, so the fourth counter would be a fifth set of terms on a negotiation that has
#: already gone on longer than either party asked for.
MAX_THREAD_DEPTH: Final = 3


def proposing_party(thread_depth: int) -> Proposer:
    """Whose terms these are, told by how deep in the thread they are.

    Parity rather than a stored answer, so that the question has exactly one source. A counter
    that recorded the proposer as well as the depth would be two records of one fact, and the
    authorization check that follows would be reading whichever one it was written against.
    """
    return Proposer.BUYER if thread_depth % 2 == 0 else Proposer.SELLER


class Offer(TimestampMixin, Base):
    """One set of terms, put forward by one side, on one listing."""

    __tablename__ = "offers"

    # A negotiation move writes the row, and `updated_at` is written by the database clock.
    # With this set, that value comes back in the same statement instead of being left to load
    # on first read, which in an async request would be an un-awaited query.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The listing being bid on. Every rule about what may be offered — the price ceiling, the
    # minimum weight, whether the listing is even live — is a rule about this row, which is
    # why an offer is never held without it.
    listing_id: Mapped[UUID] = mapped_column(
        ForeignKey("listings.id", name="fk_offers_listing_id_listings"),
        nullable=False,
    )
    # The buyer in the negotiation. Held constant for the whole thread, including the seller's
    # counters: it names who the negotiation is *with*, not who is speaking in it. Which side
    # is speaking is the depth's job.
    buyer_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", name="fk_offers_buyer_id_users"),
        nullable=False,
    )
    # The thread this offer belongs to. An original generates its own, so that every row in a
    # negotiation can be found by the same question.
    thread_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    # How many answers deep this is. Zero is a buyer's opening bid; see `proposing_party`.
    thread_depth: Mapped[int] = mapped_column(
        SmallInteger(), nullable=False, server_default=text("0")
    )
    quantity_kg: Mapped[int] = mapped_column(Integer(), nullable=False)
    # Santim, as money is held everywhere else here: an integer, never a float, so no rounding
    # question can arise from the arithmetic.
    price_santim: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    # When these terms stop being on the table. Read against the clock on every read rather
    # than waited for, so an offer nobody acted on expires whether or not a job got round to
    # writing that down.
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[OfferStatus] = mapped_column(
        SqlEnum(OfferStatus, name="offer_status", values_callable=stored_values),
        nullable=False,
    )
    # How the sale this offer would become is paid for, fixed when the thread opens and carried
    # by every counter in it. The default is escrow because that is what every offer predating
    # this column was; a deployment running the COD pilot writes COD on the opening bid instead.
    payment_mode: Mapped[PaymentMode] = mapped_column(
        SqlEnum(PaymentMode, name="payment_mode", values_callable=stored_values),
        nullable=False,
        server_default=PaymentMode.ESCROW.value,
    )
    # Carried because §11.1 gives every mutable business entity one. Nothing in this slice
    # edits an offer's terms: a change of terms is a new offer, which is what a counter is.
    version: Mapped[int] = mapped_column(Integer(), server_default="0", nullable=False)
