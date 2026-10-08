"""Turning a parsed SMS command into a domain action and a reply to render.

The dispatcher is where the vocabulary of §14.4 meets the services the API already uses. It
holds no session, no repository and no provider: everything it needs from the platform is a
method on :class:`SmsCommandDeps`, implemented on the application side. That is deliberate —
the grammar and the reply choices are shared and testable in isolation, while the database
and the HTTP request stay where they belong.

Two decisions are worth stating rather than leaving to be inferred.

A reply is a template id and a render context, never a string. What a trader reads is
decided by the i18n registry and the trader's own language, not by this module; this module
only decides which message is owed and what fills its placeholders. A string here would be a
second copy of the copy.

The idempotency key is made once, by the caller, from the sender and the raw text, and
handed through unchanged. Two identical bodies from one handset are one intent, and every
mutation the SMS path performs is asked for under that one key — so a retried webhook cannot
post the same listing twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from packages.shared.errors import ApiError
from packages.shared.i18n.languages import Language
from packages.shared.messaging.action_tokens import (
    DELIVERY_CONFIRM,
    OFFER_ANSWER,
    parse_purpose,
)
from packages.shared.messaging.inbound import (
    CommandKind,
    ParsedCommand,
    ParsedUnknown,
)

#: The USSD short code every reply may quote. §14.3's menus live behind it.
USSD_SHORT_CODE: str = "*851#"

#: The delivery option the SMS accept path prefers when a listing offers it.
_PREFERRED_DELIVERY: str = "PICKUP"


@dataclass(frozen=True, slots=True)
class Trader:
    """The sender, as the messaging path may know them."""

    id: str
    language: Language
    region_code: str | None
    woreda_id: int | None
    kebele: str | None
    business_name: str | None
    display_name: str | None


@dataclass(frozen=True, slots=True)
class Listing:
    """A listing, cut down to what an SMS command needs from it."""

    id: str
    seller_id: str
    crop_id: int
    short_code: str
    delivery_options: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Offer:
    """An offer, with its status already judged against the clock."""

    id: str
    listing_id: str
    buyer_id: str
    price_santim: int
    status: str


@dataclass(frozen=True, slots=True)
class Order:
    """An order, with its status already judged against the clock."""

    id: str
    short_code: str
    status: str
    buyer_id: str
    seller_id: str


class SmsCommandDeps(Protocol):
    """Everything the dispatcher needs the platform to do, said in domain terms."""

    async def crop_id_for_name(self, name: str) -> int | None: ...

    async def crop_display_name(self, crop_id: int, language: Language) -> str | None: ...

    async def latest_price_santim(
        self, crop_id: int, region_code: str | None, as_of: date
    ) -> int | None: ...

    async def find_listing_by_short_code(self, short_code: str) -> Listing | None: ...

    async def find_listing_by_id(self, listing_id: str) -> Listing | None: ...

    async def find_offer(self, offer_id: str) -> Offer | None: ...

    async def find_order_by_short_code(self, short_code: str, viewer_id: str) -> Order | None: ...

    async def find_order_by_id(self, order_id: str, viewer_id: str) -> Order | None: ...

    async def create_listing(self, seller: Trader, body: dict[str, Any], key: str) -> str: ...

    async def place_offer(
        self, listing: Listing, buyer: Trader, quantity_kg: int, price_santim: int, key: str
    ) -> None: ...

    async def accept_offer(
        self, offer: Offer, trader: Trader, body: dict[str, Any], key: str
    ) -> None: ...

    async def reject_offer(self, offer: Offer, trader: Trader, key: str) -> None: ...

    async def cancel_order(self, order: Order, trader: Trader, key: str) -> None: ...

    async def confirm_delivery(self, order: Order, trader: Trader, key: str) -> None: ...

    async def consume_action_token(self, token: str) -> str | None: ...


def format_etb(santim: int) -> str:
    """Birr as a trader would say it: whole when whole, else up to two decimals.

    ``3250`` reads as ``32.5`` and ``3100`` as ``31``, not as ``32.50`` and ``31.00``: the
    figures come from a scale and a negotiated price, and trailing zeroes nobody typed are
    noise in a message with a character budget.
    """
    whole, fraction = divmod(santim, 100)
    if fraction == 0:
        return str(whole)
    return f"{whole}.{fraction:02d}".rstrip("0").rstrip(".")


def parse_etb_to_santim(text: str) -> int | None:
    """A birr figure as santim, or ``None`` if it is not money to two places.

    Done by string arithmetic rather than ``Decimal`` or ``float`` for the same reason all
    money in this platform is integer: ``32.5`` must become exactly ``3250`` and never a
    binary approximation of it.
    """
    whole, separator, fraction = text.partition(".")
    if not whole.isdigit():
        return None
    if separator:
        if not fraction.isdigit() or len(fraction) > 2:
            return None
        hundredths = int(fraction.ljust(2, "0"))
    else:
        hundredths = 0
    return int(whole) * 100 + hundredths


async def dispatch(
    command: ParsedCommand | ParsedUnknown,
    trader: Trader | None,
    deps: SmsCommandDeps,
    key: str,
) -> tuple[str, dict[str, str]]:
    """Do what the command asks, and name the reply that should go back.

    The reply is a template id and the values its placeholders take. Rendering and sending
    happen above this function, so nothing here needs to know a language or a gateway.
    """
    if trader is None:
        # An unregistered number gets pointed at the door rather than a menu it cannot use.
        return "register_prompt", {"ussd": USSD_SHORT_CODE}
    if isinstance(command, ParsedUnknown) or command.kind is CommandKind.HELP:
        return _help()
    if command.kind is CommandKind.PAYOUTS:
        # Payout records do not exist yet, and the honest answer to "show me my payouts" is
        # the empty history rather than a menu the sender did not ask for.
        return "payout_history_empty", {"count": "0"}
    if command.kind is CommandKind.PRICE:
        return await _price(command, trader, deps)
    if command.kind is CommandKind.POST:
        return await _post(command, trader, deps, key)
    if command.kind is CommandKind.OFFER:
        return await _offer(command, trader, deps, key)
    if command.kind is CommandKind.CANCEL:
        return await _cancel(command, trader, deps, key)
    if command.kind is CommandKind.STATUS:
        return await _status(command, trader, deps)
    if command.kind is CommandKind.YES:
        return await _yes(command, trader, deps, key)
    return await _no(command, trader, deps, key)


def _help() -> tuple[str, dict[str, str]]:
    return "help", {"ussd": USSD_SHORT_CODE}


def _invalid(token: str | None) -> tuple[str, dict[str, str]]:
    return "action_token_invalid", {"token": token or ""}


async def _price(
    command: ParsedCommand, trader: Trader, deps: SmsCommandDeps
) -> tuple[str, dict[str, str]]:
    """FR-405's price board, asked for by name."""
    crop_id = await deps.crop_id_for_name(command.crop or "")
    if crop_id is None:
        return "price_unavailable", {"crop": command.crop or ""}
    region = command.region or trader.region_code
    today = datetime.now(UTC).date()
    average = await deps.latest_price_santim(crop_id, region, today)
    if average is None:
        return "price_unavailable", {"crop": command.crop or ""}
    name = await deps.crop_display_name(crop_id, trader.language) or command.crop or ""
    # The reply quotes birr per quintal, which is the unit the price board is published in.
    return "price", {"crop": name, "price_per_quintal": f"{average // 100:,}"}


async def _post(
    command: ParsedCommand, trader: Trader, deps: SmsCommandDeps, key: str
) -> tuple[str, dict[str, str]]:
    """A listing created from one text message, with §14.4's fallbacks."""
    price_santim = parse_etb_to_santim(command.price_etb or "")
    if price_santim is None:
        return "post_failure", {"field": "price"}
    crop_id = await deps.crop_id_for_name(command.crop or "")
    if crop_id is None:
        return "post_failure", {"field": "crop"}
    if trader.region_code is None:
        return "post_failure", {"field": "region_code"}
    if trader.woreda_id is None:
        return "post_failure", {"field": "woreda_id"}
    body: dict[str, Any] = {
        "crop_id": crop_id,
        "quantity_kg": command.kg,
        "unit_price_santim": price_santim,
        "region_code": trader.region_code,
        "woreda_id": trader.woreda_id,
    }
    try:
        short_code = await deps.create_listing(trader, body, key)
    except ApiError as error:
        return "post_failure", {"field": _first_field(error)}
    return "post_success", {"code": short_code}


async def _offer(
    command: ParsedCommand, trader: Trader, deps: SmsCommandDeps, key: str
) -> tuple[str, dict[str, str]]:
    """A bid on a listing, named by the code a trader can read aloud."""
    price_santim = parse_etb_to_santim(command.price_etb or "")
    listing = await deps.find_listing_by_short_code(command.short_code or "")
    if price_santim is None or listing is None or command.kg is None:
        return _help()
    try:
        await deps.place_offer(listing, trader, command.kg, price_santim, key)
    except ApiError:
        # A bid the listing's own rules refuse is answered the same way as a word the grammar
        # does not know: with the menu, so the sender can see what is available to them.
        return _help()
    return "offer_placed", {
        "code": listing.short_code,
        "quantity_kg": str(command.kg),
        "price": format_etb(price_santim),
    }


async def _cancel(
    command: ParsedCommand, trader: Trader, deps: SmsCommandDeps, key: str
) -> tuple[str, dict[str, str]]:
    """A buyer walking away from an order that has not been paid for."""
    order = await deps.find_order_by_short_code(command.order_code or "", trader.id)
    if order is None:
        return _help()
    try:
        await deps.cancel_order(order, trader, key)
    except ApiError:
        return _help()
    return "cancel_confirmed", {"order_code": order.short_code}


async def _status(
    command: ParsedCommand, trader: Trader, deps: SmsCommandDeps
) -> tuple[str, dict[str, str]]:
    """The state of one order, for either party to it."""
    order = await deps.find_order_by_short_code(command.order_code or "", trader.id)
    if order is None:
        return _help()
    return "status", {"order_code": order.short_code, "status": order.status}


async def _yes(
    command: ParsedCommand, trader: Trader, deps: SmsCommandDeps, key: str
) -> tuple[str, dict[str, str]]:
    """Accept an offer or confirm a delivery, whichever this token unlocks."""
    purpose = await deps.consume_action_token(command.token or "")
    parsed = parse_purpose(purpose) if purpose is not None else None
    if parsed is None:
        return _invalid(command.token)
    kind, target = parsed
    if kind == OFFER_ANSWER:
        return await _accept_offer(target, command.token, trader, deps, key)
    if kind == DELIVERY_CONFIRM:
        return await _confirm_delivery(target, command.token, trader, deps, key)
    return _invalid(command.token)


async def _accept_offer(
    offer_id: str,
    token: str | None,
    trader: Trader,
    deps: SmsCommandDeps,
    key: str,
) -> tuple[str, dict[str, str]]:
    """The accept path: build the order body the SMS channel cannot ask for, or say why not."""
    offer = await deps.find_offer(offer_id)
    if offer is None:
        return _invalid(token)
    listing = await deps.find_listing_by_id(offer.listing_id)
    if listing is None:
        return _invalid(token)
    delivery_method = _delivery_method_for(listing)
    if delivery_method is None:
        return _invalid(token)
    if trader.woreda_id is None:
        return "profile_incomplete_for_order", {"missing": "woreda"}
    address = trader.kebele or trader.business_name or trader.display_name
    if not address:
        return "profile_incomplete_for_order", {"missing": "address"}
    body: dict[str, Any] = {
        "delivery_method": delivery_method,
        "buyer_address_woreda_id": trader.woreda_id,
        "buyer_address_text": address,
    }
    try:
        await deps.accept_offer(offer, trader, body, key)
    except ApiError:
        # An offer already closed, or one this sender may not answer, is the same sentence to
        # the trader: the token did not work.
        return _invalid(token)
    name = await deps.crop_display_name(listing.crop_id, trader.language) or ""
    return "offer_accepted", {"crop": name, "price": format_etb(offer.price_santim)}


async def _confirm_delivery(
    order_id: str,
    token: str | None,
    trader: Trader,
    deps: SmsCommandDeps,
    key: str,
) -> tuple[str, dict[str, str]]:
    """The confirm path: an order in transit becoming delivered, or a refusal."""
    order = await deps.find_order_by_id(order_id, trader.id)
    if order is None:
        return _invalid(token)
    try:
        await deps.confirm_delivery(order, trader, key)
    except ApiError:
        return _invalid(token)
    return "order_delivered", {"order_code": order.short_code}


async def _no(
    command: ParsedCommand, trader: Trader, deps: SmsCommandDeps, key: str
) -> tuple[str, dict[str, str]]:
    """Refuse an offer, if this token and this sender may."""
    purpose = await deps.consume_action_token(command.token or "")
    parsed = parse_purpose(purpose) if purpose is not None else None
    if parsed is None or parsed[0] != OFFER_ANSWER:
        return _invalid(command.token)
    offer = await deps.find_offer(parsed[1])
    if offer is None:
        return _invalid(command.token)
    try:
        await deps.reject_offer(offer, trader, key)
    except ApiError:
        return _invalid(command.token)
    listing = await deps.find_listing_by_id(offer.listing_id)
    name = (
        await deps.crop_display_name(listing.crop_id, trader.language)
        if listing is not None
        else None
    )
    return "offer_rejected", {"crop": name or ""}


def _delivery_method_for(listing: Listing) -> str | None:
    """The listing's first offered option, preferring pickup when it is on offer.

    Never invents a method: an SMS acceptance takes the goods away the way the seller said
    they could be taken, and a listing that offers none is a listing the SMS path cannot
    complete.
    """
    if _PREFERRED_DELIVERY in listing.delivery_options:
        return _PREFERRED_DELIVERY
    return listing.delivery_options[0] if listing.delivery_options else None


def _first_field(error: ApiError) -> str:
    """The field a validation refusal names, or ``listing`` when it names none.

    The order the service reports fields in is kept rather than sorted: the service reports
    the problem it decided was first, and re-sorting here would be a second opinion about
    which typo matters.
    """
    fields = error.details.get("fields")
    if isinstance(fields, dict) and fields:
        return str(next(iter(fields)))
    return "listing"
