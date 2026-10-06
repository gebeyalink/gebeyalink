"""Request and response bodies for orders.

An order is created from what the buyer chose and what the listing already says. The body
therefore carries the choice and the delivery address, and nothing else: who the seller is, what
the price is, what the fee is and what the total comes to are all decided by the listing and by
the money functions, because a body that could state them would be a body a client could lie
about. `extra="forbid"` is what makes that true rather than aspirational — a client sending
`total_santim` gets a 422 naming the field instead of a silently ignored value.

Two bodies rather than one. An acceptance already has its terms — they are the offer's — so it
takes only the delivery address. A buy-now has to state the weight it wants at the asking price,
because there was no negotiation to carry it.

The address is required on both. The order is where the goods are going, a pickup still has to be
picked up from somewhere, and the seller carrying a truckload is as entitled to a place to take
it as somebody receiving a parcel. The USSD path reads it from the buyer's own profile, which
already has one.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from apps.api.models.listing import DeliveryOption
from apps.api.models.order import OrderStatus, PaymentMode
from packages.shared.money import FeePayer


class OrderAcceptBody(BaseModel):
    """Where the goods go, on an acceptance whose price and weight are already settled.

    The weight is the offer's and the price is the offer's. Repeating either here would give a
    seller two ways to agree to something, and they would disagree the first time the two fields
    were filled in differently.
    """

    model_config = ConfigDict(extra="forbid")

    delivery_method: DeliveryOption
    buyer_address_woreda_id: int = Field(ge=1)
    buyer_address_text: str = Field(min_length=1, max_length=300)


class OrderBuyNowBody(OrderAcceptBody):
    """A purchase at the asking price, with the weight the buyer wants.

    `payment_mode` defaults to escrow, which is the only mode most deployments offer. A COD
    order is the buyer choosing to pay on delivery rather than fund the order, and it is
    gated by configuration: a mode the platform has not opened cannot be chosen, no matter
    what the body says.
    """

    quantity_kg: int = Field(ge=1, le=1_000_000)
    payment_mode: PaymentMode = PaymentMode.ESCROW


class PickupConfirmBody(BaseModel):
    """The six digits a seller reads back from a buyer at the roadside.

    Digits only, because the code is read aloud in a market by somebody who is not typing into
    a form. `extra="forbid"` is what keeps a pickup confirmation honest: a code and nothing
    else is all this move needs, and a body carrying more is the beginning of a pickup being
    confirmed by something other than the code.
    """

    model_config = ConfigDict(extra="forbid")

    pickup_code: str = Field(
        min_length=6,
        max_length=6,
        pattern="^[0-9]{6}$",
        description="The code the buyer was shown when the order was created.",
    )


class WeightBody(BaseModel):
    """The delivered weight, in whole kilograms, recorded by the seller.

    The upper bound is enforced twice — here by a flywheel that stops a nonsense figure from
    reaching the service, and again in the service against the ordered amount (a thousand
    kilos ordered can never deliver twelve hundred) — because the first bound is about the
    shape of the number and the second is about the trade it belongs to.
    """

    model_config = ConfigDict(extra="forbid")

    delivered_weight_kg: int = Field(
        ge=1, le=1_000_000, description="Whole kilograms actually handed over."
    )


class OrderResponse(BaseModel):
    """An order as it is read back.

    The money figures are the frozen ones, so a client showing "what I pay" shows what will
    actually be taken. `pickup_code` is here because it is the buyer's to keep and the seller's
    to ask for — but only to the buyer: the seller's copy of the order does not carry the code
    they are supposed to be asking it for, because a code visible to the party being verified is
    a code the verification proves nothing about. `cancel_reason` is here because "it expired"
    and "I cancelled it" are different things to the buyer looking at a list, and one word for
    both would lose that.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    short_code: str
    buyer_id: UUID
    seller_id: UUID
    listing_id: UUID
    offer_id: UUID | None
    quantity_kg: int
    unit_price_santim: int
    delivery_method: DeliveryOption
    delivery_fee_santim: int
    platform_fee_santim: int
    vat_santim: int
    total_santim: int
    buyer_address_woreda_id: int
    buyer_address_text: str
    pickup_code: str | None
    status: OrderStatus
    state_before_dispute: OrderStatus | None
    funded_at: datetime | None
    ready_at: datetime | None
    shipped_at: datetime | None
    delivered_at: datetime | None
    delivered_weight_kg: int | None
    cancel_reason: str | None
    payment_mode: PaymentMode
    # When both parties had confirmed a COD order. Null until the second confirmation, and
    # null forever on an escrow order, which is confirmed by the payment instead.
    confirmed_at: datetime | None
    # Derived, never stored: whether the seller is past the seventy-two hours the parties
    # agreed on. False until the order is confirmed, because there is no agreement to be late
    # on before both parties have signed.
    seller_delayed: bool
    # Who was paying when this order was created, and it is read back rather than recomputed for
    # the same reason the totals are not: the figure on the row is the agreement, and today's
    # configuration is somebody else's decision.
    fee_payer: FeePayer
    version: int
    created_at: datetime
    updated_at: datetime
