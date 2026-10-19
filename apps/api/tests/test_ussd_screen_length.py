"""FR-1101 (§14.3): every browse and create-listing screen fits one USSD reply, in each language.

A USSD screen is one string. The channel carries a text, not a page that scrolls, so a screen
that is too long is not wrapped — it is truncated by the network or refused, and the trader at
the far end sees half a menu. The wizard's own tests assert what a screen *says*; this file
asserts only how *long* it is, in English, Oromo and Amharic, so a screen that grows past the
channel fails here rather than on a call.

Two budgets, because the channel's alphabet decides them. A screen of GSM 7-bit characters
(the default alphabet of GSM 03.38) has the larger budget; a screen with any character outside
it is sent as UCS-2 and has the smaller one.

    182  GSM 7-bit. The channel's usual per-screen limit, and the number the crop list's
         length was first measured against.
     80  UCS-2. Ge'ez, and the `❌` reason marker, are both outside GSM 7-bit, so a screen
         carrying either is UCS-2 and only fits the smaller budget. The crop list carries
         both crop names (one of them Amharic) in every language, which is why it is paginated
         two rows at a time. The buyer's browse crop menu is the same screen behind the whole
         catalogue, so it is paginated and measured here too.

Both are the owner's figures rather than a citation: Africa's Talking's documented per-screen
limit is not recorded in this repository, and when it lands (O-2) it becomes the source for
these two numbers and for ``_NEW_LISTING_CROP_PAGE``. Only then can they be adjusted down (or
up) with a reference instead of a guess.

One class of screen is deliberately *not* measured here. A refusal is the prompt plus a reason
line the renderer prefixes with ``❌ ``; that marker alone makes the screen UCS-2, and the
longest refusal re-render (an Oromo harvest refusal) is about 130 characters — over the UCS-2
budget. Those reason lines predate this wizard, so they are recorded as an open finding rather
than quietly widened into the budget here.

Marked unit: this renders templates, it does not read a database.
"""

from __future__ import annotations

import pytest

from apps.api.services.ussd_gateway import _BROWSE_CROP_PAGE, _NEW_LISTING_CROP_PAGE
from packages.shared.i18n.languages import Language
from packages.ussd.render import render, render_screen

pytestmark = pytest.mark.unit

#: The USSD per-screen budgets: the GSM 7-bit limit and the UCS-2 limit. See the module docstring
#: for why there are two and where the numbers come from (and the O-2 follow-up that settles them).
GSM7_SCREEN_LIMIT = 182
UCS2_SCREEN_LIMIT = 80

#: The GSM 03.38 default alphabet, plus the escape and form-feed the same table carries. A screen
#: whose every character is in here is sent as GSM 7-bit; one character outside it makes it UCS-2.
_GSM7 = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

#: The catalogue's nineteen weighed crops, in the order the wizard shows them to a market where
#: nothing is listed yet (the zero-kilo tie breaks on the catalogue number). Transcribed rather
#: than read from Postgres so the length guard stays a render test; the seed itself is pinned by
#: `test_reference_seed_integrity.py`.
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


def _budget(reply: str) -> int:
    """The limit this reply is held to: the larger one only if it is pure GSM 7-bit."""
    return GSM7_SCREEN_LIMIT if all(char in _GSM7 for char in reply) else UCS2_SCREEN_LIMIT


def _fits(name: str, language: Language, reply: str) -> None:
    limit = _budget(reply)
    assert len(reply) <= limit, (
        f"{name} in {language.value} is {len(reply)} characters, over its {limit}-character "
        f"USSD budget: {reply!r}"
    )


def _crop_rows(start: int, language: Language) -> str:
    """The rows the gateway builds for the crop page at ``start``, "more" row and all."""
    window = CROPS[start : start + _NEW_LISTING_CROP_PAGE]
    rows = [f"{index}. {en} {am}" for index, (en, am) in enumerate(window, start=1)]
    if start + _NEW_LISTING_CROP_PAGE < len(CROPS):
        rows.append(f"{len(rows) + 1}. {render('ussd_new_listing_more', language, {})}")
    return "\n".join(rows)


#: The wizard's non-list screens: the two entry menus and the five steps that are prompts or
#: outcomes rather than a page of rows. Everything the create-listing path can show a trader.
WIZARD_SCREENS: tuple[tuple[str, tuple[str, ...], dict[str, str]], ...] = (
    ("main menu", ("ussd_main_menu",), {}),
    ("listings menu", ("ussd_listings_menu",), {"active_count": "0", "paused_count": "0"}),
    ("quantity", ("ussd_quantity_prompt",), {}),
    ("price", ("ussd_price_prompt",), {}),
    ("harvest", ("ussd_new_listing_harvest",), {}),
    ("delivery", ("ussd_new_listing_delivery",), {}),
    ("delivery fee", ("ussd_new_listing_delivery_fee",), {}),
    ("published", ("ussd_new_listing_published",), {"code": "K7T2QX"}),
)


@pytest.mark.parametrize("language", list(Language))
def test_every_wizard_screen_fits_its_ussd_budget(language: Language) -> None:
    """The entry menus and the five non-list steps fit, in each of the three languages."""
    for name, ids, context in WIZARD_SCREENS:
        reply = f"CON {render_screen(ids, language, context)}"
        _fits(name, language, reply)


@pytest.mark.parametrize("language", list(Language))
def test_every_crop_page_fits_its_ussd_budget(language: Language) -> None:
    """Every page of the crop menu fits — the first, every middle page and the short last one.

    The whole nineteen-crop list is the screen this work exists for; it cannot be rendered here
    as it is not a page. What is rendered is each page the gateway now produces, which is the
    screen a trader actually receives.
    """
    for start in range(0, len(CROPS), _NEW_LISTING_CROP_PAGE):
        body = render_screen(
            ("ussd_crop_menu", "ussd_rows"), language, {"rows": _crop_rows(start, language)}
        )
        _fits(f"crop page at row {start + 1}", language, f"CON {body}")


def _browse_crop_rows(start: int, language: Language) -> str:
    """The rows the buyer's browse menu builds for the page at ``start``, "more" row and all.

    A browse row carries the one name the trader's language asks for rather than both: Amharic
    for an Amharic screen, and English for English and Oromo, whose catalogue rows carry no
    Oromo name and so fall back to the English one.
    """
    window = CROPS[start : start + _BROWSE_CROP_PAGE]
    name = 1 if language is Language.AM else 0
    rows = [f"{index}. {pair[name]}" for index, pair in enumerate(window, start=1)]
    if start + _BROWSE_CROP_PAGE < len(CROPS):
        rows.append(f"{len(rows) + 1}. {render('ussd_new_listing_more', language, {})}")
    return "\n".join(rows)


@pytest.mark.parametrize("language", list(Language))
def test_every_browse_crop_page_fits_its_ussd_budget(language: Language) -> None:
    """The buyer's browse menu is the wizard's screen behind a different list, and fits too.

    Same heading, same "more" line and the same Ge'ez constraint, so the same two rows a page.
    """
    for start in range(0, len(CROPS), _BROWSE_CROP_PAGE):
        body = render_screen(
            ("ussd_crop_menu", "ussd_rows"), language, {"rows": _browse_crop_rows(start, language)}
        )
        _fits(f"browse crop page at row {start + 1}", language, f"CON {body}")


def test_the_budgets_are_the_two_the_channel_uses() -> None:
    """A guard on the guard: the larger budget is the GSM one and the smaller is UCS-2.

    A screen of ASCII is held to 182; add one Ge'ez character and the same screen is held to 80.
    If these ever collapse to one number, the crop list would have been paginated for nothing.
    """
    assert _budget("plain ascii") == GSM7_SCREEN_LIMIT
    assert _budget("ጤፍ") == UCS2_SCREEN_LIMIT
