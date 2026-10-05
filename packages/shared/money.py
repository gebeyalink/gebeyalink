"""Order money, in integer santim.

Every amount an order carries is an integer number of santim — one hundredth of a birr — from
the subtotal to the seller's payout. Not a float that happens to look whole: the specification
asks for integer santim, and a float in this module would be a rounding error waiting for a
subtotal large enough or awkward enough to expose it. Two percent of a whole number of santim
is not a whole number of santim, and neither is fifteen percent of a fee, so the arithmetic
here is done on exact integer fractions and the result is rounded once, deliberately, at the
end.

Which side funds the platform fee is configuration rather than arithmetic, and the three
settings are not variants of one another: they decide who pays, and the buyer's total and the
seller's payout move together in every branch so that the two always reconcile. The fee the
platform earns is the same in all three; only the funding side differs, which is why the split
between `buyer_fee` and `seller_fee` is stated rather than left to be inferred from the totals.

Rounding is **half-up**: a half always rounds away from zero, so 2.5 santim becomes 3 and
3.5 becomes 4. This is stated here because it is a convention rather than a law, and because
the obvious implementation of "round half up" in Python is not half-up at all.
`round(12_525 * 0.02)` is **250**, not 251: `round` rounds half to *even*, and 250 is even. The
same expression is also a float multiplication, so the value being rounded is not 250.5 but
whatever 0.02 happens to be in binary. Both faults point the same way and both are silent,
which is why `round_half_up` below is written in integers and tested on exact halves rather
than trusted. Half-up is chosen over half-to-even because half-to-even makes a fee depend on
the parity of the digit in front of the point — a trader cannot predict a charge, and a
platform that cannot predict its own revenue is a platform that cannot reconcile.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

#: One birr. Named because the alternative is a bare 100 sitting in an arithmetic expression,
#: which is a hundred what?
SANTIM_PER_ETB: Final = 100

#: Two percent of the subtotal, as the exact fraction it is. Written `2/100` rather than as
#: 0.02 because 0.02 has no exact binary representation: the nearest double to it is a hair
#: above the real value, so `round(subtotal * 0.02)` rounds a number that was never exactly
#: two percent of anything.
PLATFORM_FEE_PERCENT: Final = 2
PLATFORM_FEE_PARTS: Final = 100

#: Fifteen percent VAT, charged on the platform fee and not on the subtotal.
VAT_PERCENT: Final = 15
VAT_PARTS: Final = 100

#: Five birr. The floor exists because two percent of a small order is less than the cost of
#: collecting it; without it the smallest orders would carry a fee too small to be worth a
#: ledger entry.
PLATFORM_FEE_MINIMUM: Final = 5 * SANTIM_PER_ETB

#: Five hundred birr. This is the figure that decides what a large order costs: two percent of
#: the hundred-thousand-birr order in the worked example would be 2,000.00 ETB, and the rule
#: caps it at 500.00.
PLATFORM_FEE_MAXIMUM: Final = 500 * SANTIM_PER_ETB


class FeePayer(StrEnum):
    """Which side of the order funds the platform fee and its VAT.

    Read from configuration, because it is a commercial decision rather than a calculation:
    the same arithmetic holds whichever side is chosen, and only the split moves. It is a
    string enum so that the setting can be written `SPLIT_50` in an environment variable and
    stored in that form, without a mapping table translating it on the way in.
    """

    BUYER = "BUYER"
    SELLER = "SELLER"
    SPLIT_50 = "SPLIT_50"


@dataclass(frozen=True, slots=True)
class OrderTotals:
    """Every amount an order carries, all in santim, none of them derived twice.

    The two fee shares are carried beside the totals rather than left to be read back out of
    them. The ledger needs the seller's share in isolation — a payout is `subtotal` minus the
    seller's share of the fee, not minus whatever the buyer happened to be charged — and
    stating both shares makes the reconciliation a fact about this object rather than an
    arithmetic accident a reader has to reproduce.
    """

    quantity_kg: int
    unit_price_santim: int
    delivery_fee: int
    subtotal: int
    platform_fee: int
    vat: int
    fee_vat: int
    buyer_fee: int
    seller_fee: int
    buyer_total: int
    seller_payout: int
    fee_payer: FeePayer


def round_half_up(numerator: int, denominator: int) -> int:
    """`numerator / denominator` rounded half away from zero, in integers.

    The one rounding rule in this module, used by every percentage and by the even split, so
    that there is a single convention rather than a rounding decision per formula. Half-up is
    the convention; see the module docstring for why it is this one and not Python's.

    The identity `(2n + d) // (2d)` is exact for integers of any size, because doubling before
    the floor division moves the halfway point from a fraction that a floor would discard to an
    integer that a floor keeps. On an exact half it lands one above the truncated value, which
    is the definition of rounding up; below the half it lands on the truncated value; and above
    it lands on the next integer, as rounding should.

    Money here is never negative — a subtotal, a fee and a delivery charge are all amounts
    paid — so half away from zero and half up are the same rule, and a negative numerator is
    refused rather than quietly rounded towards zero.
    """
    if denominator <= 0:
        raise ValueError(f"denominator must be positive, got {denominator}")
    if numerator < 0:
        raise ValueError(f"money must not be negative, got {numerator}")
    return (2 * numerator + denominator) // (2 * denominator)


def clamp(value: int, lowest: int, highest: int) -> int:
    """Hold a value inside its bounds, floor first."""
    return min(max(value, lowest), highest)


def platform_fee_for(subtotal: int) -> int:
    """Two percent of the subtotal, bounded below by five birr and above by five hundred.

    The bounds are part of the fee rather than a display of it, so they are applied to the
    rounded figure: a subtotal whose two percent is 499.6 santim rounds up to 500 and meets the
    floor exactly, and one whose two percent is 50,000.4 is charged the ceiling of 50,000. Both
    edges are inclusive — a subtotal of exactly 25,000 santim is two percent of exactly 500 and
    pays 500, and a subtotal of exactly 2,500,000 santim pays the ceiling rather than 50,000.4.
    """
    two_percent = round_half_up(subtotal * PLATFORM_FEE_PERCENT, PLATFORM_FEE_PARTS)
    return clamp(two_percent, PLATFORM_FEE_MINIMUM, PLATFORM_FEE_MAXIMUM)


def vat_for(platform_fee: int) -> int:
    """Fifteen percent of the platform fee.

    VAT is charged on the fee and never on the subtotal, which is what keeps the fee's own
    bounded range meaningful: the VAT on the ceiling is 7,500 santim and not fifteen percent of
    a hundred thousand birr. A fee that has been clamped upwards by the floor still carries its
    VAT in full, because the floor is the fee.
    """
    return round_half_up(platform_fee * VAT_PERCENT, VAT_PARTS)


def fee_vat_for(platform_fee: int) -> int:
    """The fee with its VAT on top: what the platform collects before it is divided up."""
    return platform_fee + vat_for(platform_fee)


def order_totals(
    quantity_kg: int,
    unit_price_santim: int,
    delivery_fee: int = 0,
    *,
    fee_payer: FeePayer = FeePayer.BUYER,
) -> OrderTotals:
    """Every amount an order of this shape carries, for whichever side is paying the fee.

    The quantity and the unit price are taken rather than a pre-computed subtotal so that the
    multiplication happens here, once, in the one module that is allowed to do it. The delivery
    fee belongs to neither party's share of the platform fee and is passed straight through to
    the buyer's total, since it is the cost of getting the goods to the buyer rather than a
    charge on the transaction.

    `fee_payer` defaults to the buyer because that is the configured default, and the argument
    default says so where a reader of the signature will see it.
    """
    for name, amount in (
        ("quantity_kg", quantity_kg),
        ("unit_price_santim", unit_price_santim),
        ("delivery_fee", delivery_fee),
    ):
        if amount < 0:
            raise ValueError(f"{name} must not be negative, got {amount}")

    subtotal = quantity_kg * unit_price_santim
    platform_fee = platform_fee_for(subtotal)
    vat = vat_for(platform_fee)
    fee_vat = platform_fee + vat

    if fee_payer is FeePayer.BUYER:
        buyer_fee, seller_fee = fee_vat, 0
    elif fee_payer is FeePayer.SELLER:
        buyer_fee, seller_fee = 0, fee_vat
    else:
        # The buyer's share is rounded up from the half, and the seller's is whatever is left,
        # so the two add back to the fee exactly. Splitting the fee in half twice, once per
        # side, would leave a santim unaccounted for on every odd fee, and an amount of VAT
        # that belongs to neither party is an amount nobody can book.
        buyer_fee = round_half_up(fee_vat, 2)
        seller_fee = fee_vat - buyer_fee

    return OrderTotals(
        quantity_kg=quantity_kg,
        unit_price_santim=unit_price_santim,
        delivery_fee=delivery_fee,
        subtotal=subtotal,
        platform_fee=platform_fee,
        vat=vat,
        fee_vat=fee_vat,
        buyer_fee=buyer_fee,
        seller_fee=seller_fee,
        buyer_total=subtotal + delivery_fee + buyer_fee,
        seller_payout=subtotal - seller_fee,
        fee_payer=fee_payer,
    )
