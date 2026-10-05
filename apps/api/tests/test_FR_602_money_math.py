"""The money an order carries, checked without a database because it is arithmetic.

Every assertion here is on a number. There is no HTTP, no session and no table, because the
questions are the ones a spreadsheet answers badly: which side pays, what happens to a half
santim, and whether the buyer's charge and the seller's payout still add up to the fee once a
delivery charge is folded in. The two worked examples are the specification's own arithmetic
at full size, and the rest are the edges, where a total silently differs from the policy that
produced it.

The reconciliation assertion at the end is the important one. Branching on `fee_payer` is
where a change could plausibly stop conserving money, so it is asserted as an identity over a
range of orders rather than on two lucky examples.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

# No HTTP, no database: this file exercises the arithmetic alone.
pytestmark = pytest.mark.unit

from apps.api.core.config import Settings  # noqa: E402
from packages.shared.money import (  # noqa: E402
    PLATFORM_FEE_MAXIMUM,
    PLATFORM_FEE_MINIMUM,
    FeePayer,
    OrderTotals,
    clamp,
    fee_vat_for,
    order_totals,
    platform_fee_for,
    round_half_up,
    vat_for,
)


def test_the_buyer_pays_the_fee_on_a_hundred_thousand_birr_order() -> None:
    """A hundred thousand birr, fee payer BUYER: the fee is capped, the VAT is not.

    Two percent of 100,000.00 ETB is 2,000.00 ETB, which is four times the ceiling, so the cap
    is what the fee actually is. VAT is fifteen percent of that capped fee rather than fifteen
    percent of the two percent, and the seller still receives the subtotal untouched.
    """
    totals = order_totals(quantity_kg=100, unit_price_santim=100_000, fee_payer=FeePayer.BUYER)

    assert totals.subtotal == 10_000_000
    assert totals.platform_fee == 50_000
    assert totals.vat == 7_500
    assert totals.fee_vat == 57_500
    assert totals.buyer_fee == 57_500
    assert totals.seller_fee == 0
    assert totals.buyer_total == 10_057_500
    assert totals.seller_payout == 10_000_000


def test_the_seller_pays_the_fee_and_the_delivery_charge_still_reaches_the_buyer() -> None:
    """Ten thousand birr with a hundred birr delivery, fee payer SELLER.

    The delivery charge is the cost of moving the goods to the buyer, so it is the buyer's in
    every branch; only the fee moves. The seller's payout is the subtotal less the fee and its
    VAT, and the buyer's total is not touched by any of it.
    """
    totals = order_totals(
        quantity_kg=100,
        unit_price_santim=10_000,
        delivery_fee=10_000,
        fee_payer=FeePayer.SELLER,
    )

    assert totals.subtotal == 1_000_000
    assert totals.delivery_fee == 10_000
    assert totals.platform_fee == 20_000
    assert totals.vat == 3_000
    assert totals.fee_vat == 23_000
    assert totals.buyer_fee == 0
    assert totals.seller_fee == 23_000
    assert totals.buyer_total == 1_010_000
    assert totals.seller_payout == 977_000


def test_a_100_birr_delivery_does_not_change_the_fee() -> None:
    """The fee is a share of the subtotal, so a delivery charge cannot inflate it.

    Worth one assertion of its own because it is the mistake the formula invites: passing the
    grand total into the fee instead of the subtotal, which would tax the buyer twice over for
    a delivery they already paid for.
    """
    without = order_totals(quantity_kg=50, unit_price_santim=20_000, fee_payer=FeePayer.BUYER)
    with_delivery = order_totals(
        quantity_kg=50, unit_price_santim=20_000, delivery_fee=50_000, fee_payer=FeePayer.BUYER
    )

    assert with_delivery.platform_fee == without.platform_fee
    assert with_delivery.fee_vat == without.fee_vat
    assert with_delivery.buyer_total == without.buyer_total + 50_000
    assert with_delivery.seller_payout == without.seller_payout


@pytest.mark.parametrize(
    ("subtotal", "fee"),
    [
        # Below the floor the two percent is smaller than the cost of collecting it, so the
        # five birr minimum is what the fee is. An order of nothing still carries it: the
        # floor is the price of being on the platform, not a share of a positive amount.
        (0, PLATFORM_FEE_MINIMUM),
        (1, PLATFORM_FEE_MINIMUM),
        (1_000, PLATFORM_FEE_MINIMUM),
        (12_500, PLATFORM_FEE_MINIMUM),
        # At 24,950 the two percent is 499 and the floor is doing the work; one santim more
        # and the arithmetic alone would round up to 500 and agree with the floor.
        (24_950, PLATFORM_FEE_MINIMUM),
        (24_999, PLATFORM_FEE_MINIMUM),
        # Exactly 25,000 is exactly two percent of 500. The two rules meet here, which is the
        # whole reason this figure is named: a cent either side must still be 500, so the
        # floor does not end with a step.
        (25_000, PLATFORM_FEE_MINIMUM),
        (25_001, PLATFORM_FEE_MINIMUM),
        # Ordinary orders in between: two percent, exactly.
        (50_000, 1_000),
        (100_000, 2_000),
        (1_000_000, 20_000),
        (2_000_000, 40_000),
        # The ceiling is inclusive and the same figure on both sides of it.
        (2_499_999, PLATFORM_FEE_MAXIMUM),
        (2_500_000, PLATFORM_FEE_MAXIMUM),
        (2_500_001, PLATFORM_FEE_MAXIMUM),
        # Four times over the ceiling, as in the worked example above.
        (10_000_000, PLATFORM_FEE_MAXIMUM),
    ],
)
def test_the_fee_is_two_percent_bounded_at_both_ends(subtotal: int, fee: int) -> None:
    """The floor and the ceiling are part of the fee, not a display of it."""
    assert platform_fee_for(subtotal) == fee


def test_the_bounds_are_five_and_five_hundred_birr() -> None:
    """Stated in birr because the minimum and the maximum are commercial decisions."""
    assert (PLATFORM_FEE_MINIMUM, PLATFORM_FEE_MAXIMUM) == (500, 50_000)


@pytest.mark.parametrize(
    ("fee", "vat"),
    [
        # The floor's fee: fifteen percent of 500 is exactly 75.
        (500, 75),
        # Just above the floor the VAT is no longer a whole multiple of fifteen.
        (501, 75),
        (505, 76),
        # An exact half: 510 is a reachable fee, and fifteen percent of it is 76.5, which
        # rounds up to 77. Half-to-even would give 76 and quietly hand the half santim away,
        # so this is the case that would change a total if the rounding rule were moved.
        (510, 77),
        (511, 77),
        (520, 78),
        # Ordinary fees.
        (1_000, 150),
        (20_000, 3_000),
        (50_000, 7_500),
    ],
)
def test_vat_is_fifteen_percent_of_the_fee_rounded_half_up(fee: int, vat: int) -> None:
    """VAT is on the fee, never on the subtotal, and a half santim rounds up."""
    assert vat_for(fee) == vat


def test_vat_on_the_capped_fee_is_not_vat_on_two_percent() -> None:
    """The cap has to reach the VAT too, or a capped fee would still carry a five-figure tax."""
    assert vat_for(PLATFORM_FEE_MAXIMUM) == 7_500
    assert round_half_up(10_000_000 * 15, 100) == 1_500_000


def test_the_vat_is_charged_on_a_fee_that_the_floor_raised() -> None:
    """A floored fee is still the fee, so it still carries its VAT in full.

    Twenty santim of two percent became five hundred, and fifteen percent of five hundred is
    seventy-five. Had the VAT been taken on the unclamped two percent, a small order would pay
    a tax on a fee it was not charged.
    """
    totals = order_totals(quantity_kg=1, unit_price_santim=1_000)

    assert totals.platform_fee == 500
    assert totals.vat == 75
    assert totals.fee_vat == 575


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [
        (0, 1, 0),
        (1, 1, 1),
        (1, 2, 1),  # 0.5 -> 1
        (3, 2, 2),  # 1.5 -> 2
        (5, 2, 3),  # 2.5 -> 3; half-to-even would say 2
        (7, 2, 4),  # 3.5 -> 4; half-to-even would also say 4, so this alone proves nothing
        (5, 10, 1),  # 0.5 -> 1
        (15, 10, 2),  # 1.5 -> 2
        (25, 10, 3),  # 2.5 -> 3; half-to-even would say 2
        (35, 10, 4),  # 3.5 -> 4
        (500, 100, 5),
        (510, 100, 5),
        (511, 100, 5),
        # The exact case that Python's own round() gets wrong: 12,525 is two percent of 250.5,
        # and round(12_525 * 0.02) is 250 because 250 is even.
        (25_050, 100, 251),
        (7, 1, 7),
    ],
)
def test_halves_round_up_not_to_even(numerator: int, denominator: int, expected: int) -> None:
    """The one rounding rule, on the exact halves where the alternatives disagree."""
    assert round_half_up(numerator, denominator) == expected


@pytest.mark.parametrize(("numerator", "denominator"), [(-1, 2), (1, 0), (1, -2)])
def test_rounding_refuses_a_share_it_cannot_round(numerator: int, denominator: int) -> None:
    """A negative amount or a zero divisor is a programming error, not a rounding question."""
    with pytest.raises(ValueError):
        round_half_up(numerator, denominator)


def test_clamp_holds_a_value_inside_its_bounds() -> None:
    """The bounds are inclusive at both ends, in both directions."""
    assert clamp(500, 500, 50_000) == 500
    assert clamp(50_000, 500, 50_000) == 50_000
    assert clamp(499, 500, 50_000) == 500
    assert clamp(50_001, 500, 50_000) == 50_000
    assert clamp(1_000, 500, 50_000) == 1_000


def test_the_fee_with_its_vat_is_the_fee_plus_the_tax() -> None:
    """The combined figure is a sum, so it can never disagree with its two parts."""
    for subtotal in (0, 1_000, 25_000, 1_000_000, 2_500_000, 10_000_000):
        fee = platform_fee_for(subtotal)

        assert fee_vat_for(fee) == fee + vat_for(fee)


@pytest.mark.parametrize(
    ("subtotal", "expected_buyer_fee", "expected_seller_fee"),
    [
        # 25,500 is two percent of 510, and 510 carries an exact half of VAT: 77. So the fee
        # with VAT is 587, an odd number, and the split has to decide what to do with the half.
        (25_500, 294, 293),
        # 500 + 75 = 575, odd in the other direction: the same half santim, same convention.
        (25_000, 288, 287),
        # 100,000 is two percent of 2,000; 2,000 + 300 VAT is 2,300, an even number, so this
        # split needs no convention at all. Worth asserting: the awkward cases above must not
        # have quietly made every order odd.
        (100_000, 1_150, 1_150),
        # The capped fee from the worked example: 50,000 + 7,500 = 57,500 is exactly even.
        (10_000_000, 28_750, 28_750),
    ],
)
def test_an_even_split_gives_the_odd_santim_to_the_buyer(
    subtotal: int, expected_buyer_fee: int, expected_seller_fee: int
) -> None:
    """Half of an odd fee+VAT rounds up for the buyer, and the seller takes the remainder.

    Rounding both halves independently would leave a santim belonging to neither party, and a
    santim that belongs to nobody is a santim the ledger cannot book. The buyer's half is
    rounded and the seller's is whatever is left, so the two always add back to the fee.
    """
    totals = order_totals(quantity_kg=1, unit_price_santim=subtotal, fee_payer=FeePayer.SPLIT_50)

    assert (totals.buyer_fee, totals.seller_fee) == (expected_buyer_fee, expected_seller_fee)
    assert totals.buyer_fee + totals.seller_fee == totals.fee_vat


@pytest.mark.parametrize(
    ("fee_payer", "expected_buyer_fee", "expected_seller_fee"),
    [
        (FeePayer.BUYER, 57_500, 0),
        (FeePayer.SELLER, 0, 57_500),
        (FeePayer.SPLIT_50, 28_750, 28_750),
    ],
)
def test_the_whole_fee_is_one_side_or_the_other(
    fee_payer: FeePayer, expected_buyer_fee: int, expected_seller_fee: int
) -> None:
    """One payer means one side funds all of it; a split means neither side does."""
    totals = order_totals(quantity_kg=100, unit_price_santim=100_000, fee_payer=fee_payer)

    assert (totals.buyer_fee, totals.seller_fee) == (expected_buyer_fee, expected_seller_fee)
    assert totals.buyer_fee + totals.seller_fee == totals.fee_vat


@pytest.mark.parametrize("fee_payer", list(FeePayer))
@pytest.mark.parametrize("subtotal", [0, 1_000, 25_000, 25_500, 1_000_000, 10_000_000])
@pytest.mark.parametrize("delivery_fee", [0, 10_000, 50_000])
def test_the_totals_reconcile_in_every_branch(
    fee_payer: FeePayer, subtotal: int, delivery_fee: int
) -> None:
    """What the buyer pays over the goods and the seller gives up over the goods is the fee.

    Read as an identity rather than as two expected numbers: for every combination of payer,
    price and delivery charge, the buyer's excess and the seller's shortfall must come to the
    fee and its VAT. A branch that quietly created or destroyed a santim would satisfy any
    single worked example and fail this.
    """
    totals = order_totals(
        quantity_kg=1, unit_price_santim=subtotal, delivery_fee=delivery_fee, fee_payer=fee_payer
    )

    buyer_excess = totals.buyer_total - totals.delivery_fee - totals.subtotal
    seller_shortfall = totals.subtotal - totals.seller_payout

    assert buyer_excess + seller_shortfall == totals.fee_vat
    assert buyer_excess == totals.buyer_fee
    assert seller_shortfall == totals.seller_fee


@pytest.mark.parametrize("fee_payer", list(FeePayer))
def test_the_ledger_is_the_same_whichever_side_pays(fee_payer: FeePayer) -> None:
    """Only the funding side moves. The fee and its VAT are the platform's income either way.

    Same subtotal, three settings, identical fee and VAT: a platform that booked a different
    revenue figure depending on who was paying would be reporting on its own policy rather
    than on its business.
    """
    totals = order_totals(quantity_kg=100, unit_price_santim=10_000, fee_payer=fee_payer)

    assert (totals.subtotal, totals.platform_fee, totals.vat, totals.fee_vat) == (
        1_000_000,
        20_000,
        3_000,
        23_000,
    )


def test_delivery_is_charged_to_the_buyer_in_every_branch() -> None:
    """The delivery fee is a cost of getting goods to the buyer, so it never moves to the seller."""
    for fee_payer in FeePayer:
        with_delivery = order_totals(
            quantity_kg=10, unit_price_santim=100_000, delivery_fee=25_000, fee_payer=fee_payer
        )

        assert with_delivery.buyer_total - with_delivery.subtotal - with_delivery.buyer_fee == (
            25_000
        )
        assert with_delivery.seller_payout == with_delivery.subtotal - with_delivery.seller_fee


def test_a_zero_delivery_charge_is_allowed_and_changes_nothing_but_the_total() -> None:
    """Pickup carries no delivery charge, and a pickup order still has a buyer's total."""
    totals = order_totals(quantity_kg=100, unit_price_santim=10_000)

    assert totals.delivery_fee == 0
    assert totals.buyer_total == totals.subtotal + totals.fee_vat


def test_the_buyer_pays_by_default() -> None:
    """The default is the buyer, so a caller that forgets to say pays the way the spec says.

    Asserted on the function signature as well as on the setting, because a default that lives
    in configuration but not in the call is a default half the codebase can forget.
    """
    totals = order_totals(quantity_kg=100, unit_price_santim=100_000)

    assert totals.fee_payer is FeePayer.BUYER
    assert totals.buyer_total == totals.subtotal + totals.fee_vat
    assert totals.seller_payout == totals.subtotal


def test_the_configured_default_is_the_buyer() -> None:
    """The setting itself defaults to BUYER, so a deployment that configures nothing still does.

    Read from the model rather than from a constructed instance: building `Settings` needs a
    database URL and a signing key, and a rule about money should not need a database to check.
    """
    assert Settings.model_fields["fee_payer"].default is FeePayer.BUYER


@pytest.mark.parametrize("value", ["BUYER", "SELLER", "SPLIT_50"])
def test_the_setting_is_written_in_plain_words(value: str) -> None:
    """`SPLIT_50` is spelled the same in an environment variable as it is in code."""
    assert FeePayer(value).value == value


def test_there_are_exactly_three_ways_to_pay() -> None:
    """Buyer, seller, or half each. A fourth would need a spec, not a preference."""
    assert [payer.value for payer in FeePayer] == ["BUYER", "SELLER", "SPLIT_50"]


@pytest.mark.parametrize(
    ("quantity_kg", "unit_price_santim", "delivery_fee"),
    [
        (-1, 1_000, 0),
        (1, -1_000, 0),
        (1, 1_000, -1),
        (-5, -5, -5),
    ],
)
def test_a_negative_amount_is_refused(
    quantity_kg: int, unit_price_santim: int, delivery_fee: int
) -> None:
    """Money is an amount paid, so a negative one is a caller error rather than a discount."""
    with pytest.raises(ValueError, match="must not be negative"):
        order_totals(
            quantity_kg=quantity_kg, unit_price_santim=unit_price_santim, delivery_fee=delivery_fee
        )


def test_the_totals_are_frozen() -> None:
    """A total that has been signed for the ledger should not be editable afterwards."""
    totals = order_totals(quantity_kg=1, unit_price_santim=1_000)

    with pytest.raises(FrozenInstanceError):
        totals.buyer_total = 0  # type: ignore[misc]


def test_a_zero_quantity_order_is_free_and_still_carries_the_fee() -> None:
    """No goods, no subtotal, and the five birr minimum is charged anyway.

    Not reachable from a real order — there is nothing to deliver — but it is the clearest
    statement of what the floor is: it is a floor on the fee, not a share of a positive amount.
    """
    totals = order_totals(quantity_kg=0, unit_price_santim=100_000)

    assert totals.subtotal == 0
    assert totals.platform_fee == PLATFORM_FEE_MINIMUM
    assert totals.buyer_total == totals.fee_vat
    assert totals.seller_payout == 0


def test_the_totals_carry_the_inputs_they_were_built_from() -> None:
    """Quantity, unit price and the delivery charge travel with the figures they produced.

    An order that cannot say how its total was arrived at cannot be checked against a listing,
    and a listing quote and an order total that disagree is a support ticket.
    """
    totals = order_totals(
        quantity_kg=37, unit_price_santim=2_450, delivery_fee=7_500, fee_payer=FeePayer.SPLIT_50
    )

    assert totals.quantity_kg == 37
    assert totals.unit_price_santim == 2_450
    assert totals.delivery_fee == 7_500
    assert totals.fee_payer is FeePayer.SPLIT_50
    assert totals.subtotal == 37 * 2_450
    assert isinstance(totals, OrderTotals)
