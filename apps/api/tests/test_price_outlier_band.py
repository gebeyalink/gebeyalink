"""How far a price may sit from what a crop last cost, on its own.

The arithmetic is checked without a database because it is arithmetic: the question here is
where the edges of the band are, and an edge that is off by a rounding step flags good
listings and sends sellers looking for a fault that is not there.
"""

from __future__ import annotations

import pytest

# No HTTP, no database: this file exercises the comparison alone.
pytestmark = pytest.mark.unit

from apps.api.services.listing_service import is_price_outlier  # noqa: E402


@pytest.mark.parametrize(
    ("price", "average", "flagged"),
    [
        # Inside the band, including both edges: a price exactly five times the average is
        # unusual but not wrong, and flagging it would be crying wolf on a legitimate ask.
        (1_000, 1_000, False),
        (200, 1_000, False),
        (5_000, 1_000, False),
        (201, 1_000, False),
        (4_999, 1_000, False),
        # Outside it: a fifth of what the crop costs, or five times it, is a different
        # conversation from a price for the same crop.
        (199, 1_000, True),
        (5_001, 1_000, True),
        (1, 1_000, True),
        (100_000, 1_000, True),
    ],
)
def test_the_band_is_inclusive_at_both_edges(price: int, average: int, flagged: bool) -> None:
    """Stated as multiples of a published average, in santim, with no rounding in between."""
    assert is_price_outlier(price, average) is flagged


def test_no_index_means_no_flag() -> None:
    """Nothing published is not evidence of an odd price, and must not read as one."""
    assert is_price_outlier(100_000_000, None) is False


def test_a_price_far_outside_the_band_is_still_only_a_flag() -> None:
    """The function answers whether to flag. Refusing to post is somebody else's decision."""
    assert is_price_outlier(1, 1_000_000) is True


def test_the_edges_hold_for_prices_larger_than_the_average_by_a_million() -> None:
    """No float is involved, so the comparison is exact at any size a price can reach."""
    assert is_price_outlier(5_000_000_000, 1_000_000_000) is False
    assert is_price_outlier(5_000_000_001, 1_000_000_000) is True
    assert is_price_outlier(200_000_000, 1_000_000_000) is False
    assert is_price_outlier(199_999_999, 1_000_000_000) is True
