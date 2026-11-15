"""The payment mode a feature-phone channel sends when it places an offer.

The app lets a buyer choose how a sale would be paid for: `OfferCreateBody` carries a
`payment_mode`, and the domain refuses a mode the deployment has not switched on. A
feature-phone trader has nowhere to put that choice — the SMS grammar's `OFFER` is three tokens
and the USSD offer tree asks for a weight and a price, nothing more — so on those two channels
the adapter chooses on the trader's behalf, and this is the one place that decision is written.

When escrow is off, cash-on-delivery is the only mode that will be accepted, so sending nothing
and letting the escrow default apply would refuse every bid a handset made: the adapter sends
`COD` instead. When escrow is on, both modes exist and the choice is the caller's, so the
adapter sends nothing and the offer's own default applies. The adapter is a convenience, not the
enforcement point: `OfferService.place` refuses a disabled mode whatever channel it arrived on,
so a caller that reached the service with an unfunded mode would still be refused.

The flag is read through the same service as every other gate. It is not a process value any
more, so the adapter takes the buyer and the listing's region and asks the table: two
deployments running the same code can now answer differently without redeploying either.
"""

from __future__ import annotations

from uuid import UUID

from apps.api.models.order import PaymentMode
from apps.api.services.feature_flag_service import FeatureFlagService


async def adapter_offer_payment_mode(
    flags: FeatureFlagService,
    *,
    user_id: UUID,
    region_code: str | None,
) -> PaymentMode | None:
    """The mode a feature-phone adapter should put on an opening offer, or none.

    Returns `COD` when escrow is switched off for this buyer in this listing's region, because
    that is the only mode such a deployment funds; returns `None` when escrow is on, so the
    offer takes the domain's own default rather than the adapter making a choice the trader is
    entitled to make for themselves.
    """
    if await flags.is_enabled("escrow_enabled", user_id=user_id, region_code=region_code):
        return None
    return PaymentMode.COD
