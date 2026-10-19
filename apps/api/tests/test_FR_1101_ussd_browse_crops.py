"""FR-1101 (§14.3 path 2), the buyer's browse crop menu: nineteen crops, two rows a screen.

The browse path is open to anyone: a trader picks a region, picks a crop, and reads the results.
The crop menu is the screen the create-listing wizard already shows, held to the same channel
budget - a row can carry Ge'ez, so the screen is UCS-2 and only two rows fit - but the list
behind it is the whole catalogue in catalogue order rather than the crops listed most. The row
number on a page is therefore an index into that page alone and not into the whole list: these
tests pin what the first screen shows, that "more" turns to the next page, and that a crop
picked from a later page is the crop that is browsed.

Marked integration: every screen here is the real gateway reading the real catalogue.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.integration, pytest.mark.ussd]

SELLER = "+251911000001"
BUYER = "+251911000003"

#: Oromia: the region both handsets below trade in, so the seller's row is inside the filter the
#: results page applies. The region menu is the thirteen-region list the seed publishes.
REGION_KEY = "8"

#: The catalogue's nineteen weighed crops in catalogue order. Transcribed rather than read from
#: Postgres so a page's expected rows are visible in the test; the seed itself is pinned by
#: `test_reference_seed_integrity.py`, and `list_active_kg_crops` orders by the catalogue number.
CROPS: tuple[tuple[str, str], ...] = (
    ("Teff", "ጤፍ"),
    ("Wheat", "ስንዴ"),
    ("Barley", "ገብስ"),
    ("Maize", "በቆሎ"),
    ("Sorghum", "ማሽላ"),
    ("Chickpea", "ሽምብራ"),
    ("Lentil", "ምስር"),
    ("Faba bean", "በቀላ"),
    ("White pea bean", "አተር"),
    ("Niger seed", "ኑግ"),
    ("Sesame", "ሰሊጥ"),
    ("Linseed", "አሥላስ"),
    ("Coffee (red cherry)", "ቡና"),
    ("Avocado", "አቮካዶ"),
    ("Mango", "ማንጎ"),
    ("Banana", "ሙዝ"),
    ("Onion", "ሽንኩርት"),
    ("Tomato", "ቲማቲም"),
    ("Potato", "ድንች"),
)

#: The rows one browse screen shows, and the number its "more" row carries. Two, not four: a row
#: can carry Ge'ez, so the screen is UCS-2 and only two rows fit the ~80-character channel budget.
CROP_PAGE_SIZE = 2
_MORE_KEY = str(CROP_PAGE_SIZE + 1)

MAIN_MENU = "1. My Listings\n2. Browse Crops\n3. My Orders\n4. Today's Prices\n5. Help"


def _crop_page(start: int) -> str:
    """The buyer's crop screen: the page of ``CROP_PAGE_SIZE`` English names from ``start``.

    The buyer below dials in English, and the browse row carries the one name that language
    asks for. A "more" row ends every page but the last.
    """
    window = CROPS[start : start + CROP_PAGE_SIZE]
    rows = [f"{index}. {en}" for index, (en, _am) in enumerate(window, start=1)]
    if start + CROP_PAGE_SIZE < len(CROPS):
        rows.append(f"{len(rows) + 1}. More")
    return "Choose a crop:\n" + "\n".join(rows)


def _ready(client: TestClient, signed_in: Callable[..., dict[str, str]]) -> dict[str, str]:
    """Sign a seller in and give them the region and woreda a browse result carries."""
    headers = signed_in(SELLER, language="en")
    updated = client.patch(
        "/v1/me",
        headers=headers,
        json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
    )
    assert updated.status_code == 200, updated.text
    return headers


def _sign_in_buyer(
    client: TestClient, signed_in_as_buyer: Callable[..., dict[str, str]]
) -> dict[str, str]:
    headers = signed_in_as_buyer(BUYER, language="en")
    updated = client.patch(
        "/v1/me",
        headers=headers,
        json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
    )
    assert updated.status_code == 200, updated.text
    return headers


def _walk(
    client: TestClient,
    ussd_session: Callable[..., httpx.Response],
    keys: list[str],
    *,
    session_id: str,
) -> list[httpx.Response]:
    """Dial, then send each key, returning every screen in order (the dial's first)."""
    replies = [ussd_session(client, "", session_id=session_id, phone_number=BUYER)]
    for key in keys:
        replies.append(ussd_session(client, key, session_id=session_id, phone_number=BUYER))
    return replies


def test_the_browse_crop_menu_pages_two_rows_and_offers_more(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
) -> None:
    """Nineteen crops do not fit a UCS-2 screen: the first page is two crops and a "more" row."""

    _sign_in_buyer(client, signed_in_as_buyer)

    replies = _walk(client, ussd_session, ["2", REGION_KEY], session_id="ses-browse-crops")

    assert replies[0].text == f"CON {MAIN_MENU}"
    assert replies[1].text.startswith("CON Last used: Oromia\nChoose a region:")
    assert replies[2].text == f"CON {_crop_page(0)}"
    # Two crops and a "more" row, not the whole catalogue: a row can carry Ge'ez, so the screen is
    # UCS-2 and only two rows fit the channel's budget. That is the paging decision.
    assert replies[2].text.endswith(f"{_MORE_KEY}. More")


def test_the_more_row_turns_to_the_next_page_and_the_last_page_has_none(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
) -> None:
    """Each "more" press shows the next two crops; the final page holds the last crop alone."""

    _sign_in_buyer(client, signed_in_as_buyer)
    _walk(client, ussd_session, ["2", REGION_KEY], session_id="ses-browse-next")

    for start in range(CROP_PAGE_SIZE, len(CROPS), CROP_PAGE_SIZE):
        page = ussd_session(client, _MORE_KEY, session_id="ses-browse-next", phone_number=BUYER)
        assert page.text == f"CON {_crop_page(start)}"
        if start + CROP_PAGE_SIZE >= len(CROPS):
            assert not page.text.endswith("More"), page.text


def test_a_crop_picked_from_a_later_page_is_the_crop_browsed(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    ussd_session: Callable[..., httpx.Response],
) -> None:
    """A press on page two resolves against that page's crops, and finds that crop's listing."""

    seller = _ready(client, signed_in)
    created = client.post(
        "/v1/listings",
        headers=seller,
        json=listing_body(crop_id=3, delivery_options=["PICKUP"]),
    )
    assert created.status_code == 201, created.text
    _sign_in_buyer(client, signed_in_as_buyer)

    replies = _walk(
        client, ussd_session, ["2", REGION_KEY, _MORE_KEY, "1"], session_id="ses-browse-page2"
    )

    assert replies[2].text == f"CON {_crop_page(0)}"
    assert replies[3].text == f"CON {_crop_page(CROP_PAGE_SIZE)}"
    # Page two's first row is Barley, the catalogue's third crop. Row one of the whole list is
    # Teff, so a menu that resolved against the full list would show no Barley here - and no
    # result at all, because the only listing in the database is a Barley one.
    results = replies[4]
    assert results.text.startswith("CON 1. ")
    assert "Barley 2,000kg @32500 ETB — Adaa" in results.text
