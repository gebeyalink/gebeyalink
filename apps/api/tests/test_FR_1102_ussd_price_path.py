"""FR-1102, today's prices on the handset: the board for a trader, refusal for the rest.

Path 4 is open and needs no account, but the board is an average in a region, and the only
region the platform knows for a dial is the one on a profile. A registered trader whose
profile names a region reads the published averages; a handset with no account - or an
account with no region - is told the prices are not available, as a terminal screen rather
than a registration gate, because seeing why there is no price yet is not a reason to make
someone register.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

SELLER = "+251911000001"
STRANGER = "+251911000099"


def _profile_in_oromia(client: TestClient, signed_in: Callable[..., dict[str, str]]) -> None:
    headers = signed_in(SELLER, language="en")
    updated = client.patch(
        "/v1/me",
        headers=headers,
        json={"region_code": "OR", "woreda_id": 1},
    )
    assert updated.status_code == 200, updated.text


def test_a_registered_trader_in_a_region_reads_todays_board(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    publish_price_index: Callable[..., None],
    ussd_session: Callable[..., httpx.Response],
) -> None:
    """One published average for teff in Oromia is one row on the board."""

    _profile_in_oromia(client, signed_in)
    publish_price_index()

    opened = ussd_session(client, "", session_id="ses-price-1", phone_number=SELLER)
    assert opened.text == (
        "CON 1. My Listings\n2. Browse Crops\n3. My Orders\n4. Today's Prices\n5. Help"
    )

    board = ussd_session(client, "4", session_id="ses-price-1", phone_number=SELLER)
    assert board.text == "END Today's avg (Oromia): Teff 32,500/q"


def test_a_handset_with_no_region_is_told_prices_are_off_the_board(
    client: TestClient,
    ussd_session: Callable[..., httpx.Response],
) -> None:
    """The path stays open: an unregistered dial reads a terminal screen, not a gate."""

    ussd_session(client, "", session_id="ses-price-2", phone_number=STRANGER)
    ussd_session(client, "2", session_id="ses-price-2", phone_number=STRANGER)
    board = ussd_session(client, "4", session_id="ses-price-2", phone_number=STRANGER)
    assert board.text == "END Prices are not available right now."
