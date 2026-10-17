"""FR-1101 (§14.3 path 1), the create-listing wizard: five screens on a handset.

A seller's own-listing path opens with "New listing" and ends by writing a listing through the
same service the app uses. This file walks that wizard on the real engine, over the real
gateway and the real database, and then reads the row PostgreSQL holds. It pins the boundaries
a one-line prompt has - the amount ranges, the harvest day on the East Africa calendar, the
delivery fee - and the two rules that decide whether the wizard writes at all: the region and
woreda the publish needs before it starts, and the flag a price outside the market's band is
judged with rather than refused.

Marked integration: every screen here is the real gateway reading the real catalogue.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, tzinfo

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.integration, pytest.mark.ussd]

SELLER = "+251911000001"
OTHER_SELLER = "+251911000002"
BUYER = "+251911000003"

#: The East Africa offset the harvest day is read on. UTC+3, the same calendar `queues.py`
#: uses for the quiet-hours window.
EAT = timedelta(hours=3)

#: The catalogue's nineteen weighed crops in the order the wizard shows them to a market where
#: nothing is listed yet: the tie at zero listed kilos breaks on the catalogue's own number.
CROPS: list[tuple[str, str]] = [
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
]

#: The rows one crop screen shows, and the number its "more" row carries. Two, not four: a row
#: carries Ge'ez, so the screen is UCS-2 and only two rows fit the ~80-character channel budget.
CROP_PAGE_SIZE = 2
_MORE_KEY = str(CROP_PAGE_SIZE + 1)


def _crop_page(start: int) -> str:
    """The wizard's crop screen: the page of ``CROP_PAGE_SIZE`` crops from ``start``, or a More."""
    window = CROPS[start : start + CROP_PAGE_SIZE]
    rows = [f"{index}. {en} {am}" for index, (en, am) in enumerate(window, start=1)]
    if start + CROP_PAGE_SIZE < len(CROPS):
        rows.append(f"{len(rows) + 1}. More")
    return "Choose a crop:\n" + "\n".join(rows)


_PUBLISHED = re.compile(r"^END Listing (?P<code>\w+) published\. ")

MAIN_MENU = "1. My Listings\n2. Browse Crops\n3. My Orders\n4. Today's Prices\n5. Help"
LISTINGS_MENU = "1. New listing\n2. Active (0)\n3. Paused (0)"
HARVEST_MENU = "Harvest timing?\n1. Ready now\n2. Within 1 week\n3. Within 2 weeks\n4. Next month"
DELIVERY_MENU = "Delivery?\n1. Pickup only\n2. I deliver\n3. Transport partner"


def _code(reply: httpx.Response) -> str:
    match = _PUBLISHED.match(reply.text)
    assert match is not None, reply.text
    return match.group("code")


def _ready(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    phone_e164: str = SELLER,
    **profile: object,
) -> dict[str, str]:
    """Sign a seller in and give them the region and woreda a listing carries."""
    headers = signed_in(phone_e164, language="en")
    body: dict[str, object] = {"region_code": "OR", "woreda_id": 1, "kebele": "Bole"}
    body.update(profile)
    updated = client.patch("/v1/me", headers=headers, json=body)
    assert updated.status_code == 200, updated.text
    return headers


def _walk(
    client: TestClient,
    ussd_session: Callable[..., httpx.Response],
    keys: list[str],
    *,
    session_id: str,
    phone_number: str = SELLER,
) -> list[httpx.Response]:
    """Dial, then send each key, returning every screen in order (the dial's first)."""
    replies = [ussd_session(client, "", session_id=session_id, phone_number=phone_number)]
    for key in keys:
        replies.append(ussd_session(client, key, session_id=session_id, phone_number=phone_number))
    return replies


def test_the_crop_menu_pages_two_rows_and_offers_more(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
) -> None:
    """Nineteen crops do not fit a UCS-2 screen: the menu shows two and a numbered "more"."""

    _ready(client, signed_in)

    replies = _walk(client, ussd_session, ["1", "1"], session_id="ses-crnl-crops")

    assert replies[0].text == f"CON {MAIN_MENU}"
    assert replies[1].text == f"CON {LISTINGS_MENU}"
    assert replies[2].text == f"CON {_crop_page(0)}"
    # Two crops and a "more" row, not the whole catalogue: a row carries Ge'ez, so the screen is
    # UCS-2 and only two rows fit the channel's budget. That is the paging decision.
    assert replies[2].text.endswith(f"{_MORE_KEY}. More")

    more = ussd_session(client, _MORE_KEY, session_id="ses-crnl-crops", phone_number=SELLER)
    assert more.text == f"CON {_crop_page(CROP_PAGE_SIZE)}"

    picked = ussd_session(client, "1", session_id="ses-crnl-crops", phone_number=SELLER)
    assert picked.text == "CON Quantity in kg?"


def test_a_crop_reached_by_paging_is_the_crop_that_is_published(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
    stored_listings: Callable[..., list[dict[str, object]]],
) -> None:
    """A press on a later page resolves against that page's crops, and publishes the one picked."""

    _ready(client, signed_in)
    replies = _walk(
        client,
        ussd_session,
        ["1", "1", "3", "1", "500", "2500", "1", "1"],
        session_id="ses-crnl-page2",
    )
    assert replies[2].text == f"CON {_crop_page(0)}"
    assert replies[3].text == f"CON {_crop_page(CROP_PAGE_SIZE)}"
    assert replies[4].text == "CON Quantity in kg?"
    assert replies[-1].text.startswith("END Listing ")

    rows = stored_listings()
    assert len(rows) == 1
    assert rows[0]["crop_id"] == 3  # the second page's first row: Barley
    assert rows[0]["quantity_kg"] == 500


def test_the_wizard_walks_five_screens_and_writes_a_listing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
    stored_listings: Callable[[], list[dict[str, object]]],
    stored_photos: Callable[[], list[dict[str, object]]],
) -> None:
    """Crop, quantity, price, harvest, delivery: the row the database holds says all five."""

    _ready(client, signed_in)
    replies = _walk(
        client,
        ussd_session,
        ["1", "1", "1", "500", "2500", "1", "1"],
        session_id="ses-crnl-1",
    )

    assert replies[0].text == f"CON {MAIN_MENU}"
    assert replies[1].text == f"CON {LISTINGS_MENU}"
    assert replies[2].text == f"CON {_crop_page(0)}"
    assert replies[3].text == "CON Quantity in kg?"
    assert replies[4].text == "CON Price per kg in ETB?"
    assert replies[5].text == f"CON {HARVEST_MENU}"
    assert replies[6].text == f"CON {DELIVERY_MENU}"
    assert replies[7].text.startswith("END Listing ")

    code = _code(replies[-1])
    rows = stored_listings()
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "USSD"
    assert row["crop_id"] == 1
    assert row["quantity_kg"] == 500
    assert row["unit_price_santim"] == 250_000
    assert row["min_order_kg"] == 1
    assert row["region_code"] == "OR"
    assert row["woreda_id"] == 1
    assert "PICKUP" in str(row["delivery_options"])
    assert row["delivery_fee_santim"] == 0
    assert row["organic"] is False
    assert row["status"] == "ACTIVE"
    assert row["short_code"] == code
    assert row["harvest_date"] == (datetime.now(UTC) + EAT).date()
    assert stored_photos() == []


def test_a_buyer_can_find_what_the_wizard_published(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
) -> None:
    """The listing a seller wrote on a handset is in the market a buyer searches."""

    _ready(client, signed_in)
    replies = _walk(
        client,
        ussd_session,
        ["1", "1", "1", "500", "2500", "1", "1"],
        session_id="ses-crnl-find",
    )
    code = _code(replies[-1])

    buyer = signed_in_as_buyer(BUYER, language="en")
    found = client.get(
        "/v1/search/listings",
        headers=buyer,
        params={"crop_id": 1, "region_code": "OR"},
    )
    assert found.status_code == 200, found.text
    assert code in [item["short_code"] for item in found.json()["data"]]


def test_the_wizard_refuses_an_amount_outside_its_range(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    """The amount screens take a number in range and say so when the number is not."""

    _ready(client, signed_in)
    replies = _walk(
        client,
        ussd_session,
        [
            "1",
            "1",
            "1",
            "0",
            "1000001",
            "1000000",  # quantity: refused, refused, taken
            "0",
            "abc",
            "1000001",
            "2500",  # price: refused, refused, refused, taken
            "5",
            "1",  # harvest: refused, taken
            "1",  # pickup, publish
        ],
        session_id="ses-crnl-bounds",
    )
    texts = [reply.text for reply in replies]

    assert texts[3] == "CON Quantity in kg?"
    assert texts[4] == "CON ❌ Please choose one of the options.\nQuantity in kg?"
    assert texts[5] == "CON ❌ Please choose one of the options.\nQuantity in kg?"
    assert texts[6] == "CON Price per kg in ETB?"
    assert texts[7] == "CON ❌ Please choose one of the options.\nPrice per kg in ETB?"
    assert texts[8] == "CON ❌ Please enter a number.\nPrice per kg in ETB?"
    assert texts[9] == "CON ❌ Please choose one of the options.\nPrice per kg in ETB?"
    assert texts[10] == f"CON {HARVEST_MENU}"
    assert texts[11] == f"CON ❌ Please choose one of the options.\n{HARVEST_MENU}"
    assert texts[12] == f"CON {DELIVERY_MENU}"
    assert texts[13].startswith("END Listing ")

    row = stored_listings()[0]
    assert row["quantity_kg"] == 1_000_000
    assert row["unit_price_santim"] == 250_000
    assert row["harvest_date"] == (datetime.now(UTC) + EAT).date()


def test_seller_delivery_asks_a_fee_and_refuses_zero_and_nonsense(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
    stored_listings: Callable[..., list[dict[str, object]]],
) -> None:
    """A seller who delivers names a positive fee; a zero or a word is asked again."""

    _ready(client, signed_in)
    replies = _walk(
        client,
        ussd_session,
        ["1", "1", "1", "500", "2500", "1", "2"],
        session_id="ses-crnl-fee",
    )
    assert replies[-1].text == "CON Delivery fee per kg in ETB?"

    zero = ussd_session(client, "0", session_id="ses-crnl-fee", phone_number=SELLER)
    assert zero.text == "CON ❌ Please choose one of the options.\nDelivery fee per kg in ETB?"
    junk = ussd_session(client, "abc", session_id="ses-crnl-fee", phone_number=SELLER)
    assert junk.text == "CON ❌ Please enter a number.\nDelivery fee per kg in ETB?"
    paid = ussd_session(client, "50", session_id="ses-crnl-fee", phone_number=SELLER)
    assert paid.text.startswith("END Listing ")

    row = stored_listings()[0]
    assert "SELLER_DELIVERY" in str(row["delivery_options"])
    assert row["delivery_fee_santim"] == 5_000


def test_transport_partner_publishes_with_no_fee(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
    stored_listings: Callable[..., list[dict[str, object]]],
) -> None:
    """A partner carries it away, and no money attaches to that choice."""

    _ready(client, signed_in)
    replies = _walk(
        client,
        ussd_session,
        ["1", "1", "1", "500", "2500", "1", "3"],
        session_id="ses-crnl-partner",
    )
    assert replies[-1].text.startswith("END Listing ")
    row = stored_listings()[0]
    assert "TRANSPORT_PARTNER" in str(row["delivery_options"])
    assert row["delivery_fee_santim"] == 0


def test_a_replayed_final_screen_publishes_one_listing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
    stored_listings: Callable[..., list[dict[str, object]]],
) -> None:
    """The gateway re-sends the last press; the ended screen repeats, it does not publish."""

    _ready(client, signed_in)
    replies = _walk(
        client,
        ussd_session,
        ["1", "1", "1", "500", "2500", "1", "1"],
        session_id="ses-crnl-replay",
    )
    assert replies[-1].text.startswith("END Listing ")

    replay = ussd_session(client, "1", session_id="ses-crnl-replay", phone_number=SELLER)
    assert replay.text == replies[-1].text
    assert len(stored_listings()) == 1


def test_two_sellers_publishing_in_two_sessions_do_not_collide(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
    stored_listings: Callable[..., list[dict[str, object]]],
) -> None:
    """Two sessions, two sellers, two listings: the session id keeps the keys apart."""

    _ready(client, signed_in, SELLER)
    _ready(client, signed_in, OTHER_SELLER)

    first = _walk(
        client,
        ussd_session,
        ["1", "1", "1", "500", "2500", "1", "1"],
        session_id="ses-crnl-a",
        phone_number=SELLER,
    )
    second = _walk(
        client,
        ussd_session,
        ["1", "1", "1", "700", "3000", "1", "1"],
        session_id="ses-crnl-b",
        phone_number=OTHER_SELLER,
    )
    assert first[-1].text.startswith("END Listing ")
    assert second[-1].text.startswith("END Listing ")

    rows = stored_listings()
    assert len(rows) == 2
    assert {row["quantity_kg"] for row in rows} == {500, 700}
    assert len({row["short_code"] for row in rows}) == 2
    assert {row["short_code"] for row in rows} == {_code(first[-1]), _code(second[-1])}
    assert len({row["seller_id"] for row in rows}) == 2


def test_the_wizard_stops_before_it_begins_when_the_profile_has_no_region(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
) -> None:
    """A seller with nothing on their profile is told so on the menu before the wizard."""

    signed_in(SELLER, language="en")
    replies = _walk(client, ussd_session, ["1", "1"], session_id="ses-crnl-noprofile")
    assert replies[2].text == (
        "CON ❌ Your profile is missing: region\n1. New listing\n2. Active (0)\n3. Paused (0)"
    )


def test_the_wizard_names_a_missing_woreda(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
) -> None:
    """A region with no woreda is the other half of the same precondition."""

    headers = signed_in(SELLER, language="en")
    updated = client.patch("/v1/me", headers=headers, json={"region_code": "OR"})
    assert updated.status_code == 200, updated.text
    replies = _walk(client, ussd_session, ["1", "1"], session_id="ses-crnl-noworeda")
    assert "Your profile is missing: woreda" in replies[2].text


def test_the_harvest_day_follows_the_east_africa_calendar(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
    stored_listings: Callable[..., list[dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """21:30 UTC is already tomorrow in Addis; "Ready now" writes the EAT day, not the UTC one."""

    class _FrozenDateTime:
        """A `datetime` whose ``now`` is the instant this test watches the day boundary on."""

        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            moment = datetime(2026, 10, 16, 21, 30, tzinfo=UTC)
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)

    monkeypatch.setattr("apps.api.services.ussd_gateway.datetime", _FrozenDateTime)
    _ready(client, signed_in)
    replies = _walk(
        client,
        ussd_session,
        ["1", "1", "1", "500", "2500", "1", "1"],
        session_id="ses-crnl-eat",
    )
    assert replies[-1].text.startswith("END Listing ")
    assert stored_listings()[0]["harvest_date"] == date(2026, 10, 17)


def test_the_wizard_keeps_the_price_outlier_flag(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
    stored_listings: Callable[..., list[dict[str, object]]],
    publish_price_index: Callable[..., None],
) -> None:
    """A price outside the band is flagged and sold, not refused: FR-303 decides, not the wizard."""

    _ready(client, signed_in)
    publish_price_index(crop_id=1, region_code="OR", average_santim=10_000)
    replies = _walk(
        client,
        ussd_session,
        ["1", "1", "1", "500", "2500", "1", "1"],
        session_id="ses-crnl-outlier",
    )
    assert replies[-1].text.startswith("END Listing ")
    rows = stored_listings()
    assert len(rows) == 1
    assert rows[0]["price_outlier"] is True


def test_a_tier_zero_seller_may_publish_from_ussd(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
    stored_listings: Callable[..., list[dict[str, object]]],
    accounts: Callable[[], list[dict[str, object]]],
) -> None:
    """SPEC §6 lets the lowest tier sell; the wizard adds no gate to the service's own."""

    _ready(client, signed_in)
    assert "TIER_0" in str(accounts()[0]["kyc_tier"])
    replies = _walk(
        client,
        ussd_session,
        ["1", "1", "1", "500", "2500", "1", "1"],
        session_id="ses-crnl-tier0",
    )
    assert replies[-1].text.startswith("END Listing ")
    assert len(stored_listings()) == 1
