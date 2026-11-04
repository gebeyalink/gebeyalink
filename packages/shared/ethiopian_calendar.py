"""The Ethiopian calendar, converted at the channel boundary.

The API and storage are UTC-only (SPEC §4). Conversion happens at the channel boundary — the
USSD/SMS adapter, or the app's presentation layer — never in the API.

The calendar is Amete Mihret, the modern Ethiopian calendar: twelve months of thirty days and
a thirteenth month, Pagume, of five days (six in a leap year). An Ethiopian year Y is a leap
year when ``Y mod 4 == 3``. The epoch is 1 Meskerem 1 EA, which is 29 August 8 CE in the Julian
calendar and 27 August 8 CE in the proleptic Gregorian calendar Python uses. The two reference
anchors are 1 Meskerem 2015 = 11 September 2022 and 1 Meskerem 2016 = 12 September 2023.

Amharic month names are the standard ones and are treated as specification-supplied. The Oromo
and English transliterations are authored here because no single standard spelling exists; they
are unreviewed and await the O-3 review recorded in ADR 0017.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from packages.shared.i18n.languages import Language

#: 1 Meskerem 1 EA as a proleptic Gregorian date. Python's ``date`` is proleptic Gregorian,
#: so this is the one constant every conversion is measured from.
EPOCH = date(8, 8, 27)

#: Gregorian ordinal of the epoch. Kept as a plain int so the arithmetic below reads as days.
_EPOCH_ORDINAL = EPOCH.toordinal()

#: The thirteen month names, in calendar order. The Amharic names are the source; the Oromo
#: and English entries are transliterations, marked unreviewed in ADR 0017 because Oromo in
#: particular has more than one spelling convention.
MONTH_NAMES: dict[Language, tuple[str, ...]] = {
    Language.AM: (
        "መስከረም",
        "ጥቅምት",
        "ኅዳር",
        "ታኅሣሥ",
        "ጥር",
        "የካቲት",
        "መጋቢት",
        "ሚያዝያ",
        "ግንቦት",
        "ሰኔ",
        "ሐምሌ",
        "ነሐሴ",
        "ጳጉሜ",
    ),
    Language.OM: (
        "Meskerem",
        "Tikimt",
        "Hidar",
        "Tahsas",
        "Tir",
        "Yekatit",
        "Megabit",
        "Miyazya",
        "Ginbot",
        "Sene",
        "Hamle",
        "Nehase",
        "Pagume",
    ),
    Language.EN: (
        "Meskerem",
        "Tikimt",
        "Hidar",
        "Tahsas",
        "Tir",
        "Yekatit",
        "Megabit",
        "Miyazya",
        "Ginbot",
        "Sene",
        "Hamle",
        "Nehase",
        "Pagume",
    ),
}

#: The display format, and the format the parser accepts: month name, day, comma, year.
#: The month comes first so a reader sees the season before the number, and the order is the
#: same in all three languages rather than switching direction by locale.
DISPLAY_FORMAT = "{month} {day}, {year}"


def _is_leap_year(year: int) -> bool:
    """Whether an Ethiopian year has a sixth Pagume day."""
    return year % 4 == 3


def _days_in_month(year: int, month: int) -> int:
    """The number of days in one Ethiopian month."""
    if month < 13:
        return 30
    return 6 if _is_leap_year(year) else 5


def _days_before_year(year: int) -> int:
    """Days from 1 Meskerem 1 to 1 Meskerem of ``year``.

    Every year has 365 days and every year congruent to 3 modulo 4 adds one. Among the years
    ``1..year - 1`` there are ``year // 4`` such years, which is the closed form used here.
    """
    return (year - 1) * 365 + year // 4


@dataclass(frozen=True, slots=True)
class EthiopianDate:
    """A date in the Amete Mihret calendar."""

    year: int
    month: int
    day: int

    def __post_init__(self) -> None:
        if self.year < 1:
            raise ValueError("Ethiopian year must be positive")
        if not 1 <= self.month <= 13:
            raise ValueError("Ethiopian month must be between 1 and 13")
        if not 1 <= self.day <= _days_in_month(self.year, self.month):
            raise ValueError("day is outside the Ethiopian month")


def to_ethiopian(gregorian: date) -> EthiopianDate:
    """Convert a proleptic Gregorian date to the Ethiopian calendar."""
    ordinal = gregorian.toordinal() - _EPOCH_ORDINAL
    if ordinal < 0:
        raise ValueError("dates before 1 Meskerem 1 EA are outside the calendar")

    year = ordinal // 365 + 1
    while _days_before_year(year + 1) <= ordinal:
        year += 1
    while _days_before_year(year) > ordinal:
        year -= 1

    day_of_year = ordinal - _days_before_year(year)
    month = day_of_year // 30 + 1
    day = day_of_year % 30 + 1
    return EthiopianDate(year, month, day)


def from_ethiopian(ethiopian: EthiopianDate) -> date:
    """Convert an Ethiopian date to the proleptic Gregorian date the API stores."""
    ordinal = (
        _EPOCH_ORDINAL
        + _days_before_year(ethiopian.year)
        + (ethiopian.month - 1) * 30
        + (ethiopian.day - 1)
    )
    return date.fromordinal(ordinal)


def format_ethiopian(gregorian: date, language: Language) -> str:
    """Render a Gregorian date as an Ethiopian date in ``language``.

    The result is ``"<month> <day>, <year>"``, for example ``"Meskerem 1, 2016"`` or
    ``"መስከረም 1, 2016"``. The format is the same in every language; only the month name changes.
    """
    ethiopian = to_ethiopian(gregorian)
    month = MONTH_NAMES[language][ethiopian.month - 1]
    return DISPLAY_FORMAT.format(month=month, day=ethiopian.day, year=ethiopian.year)


def parse_ethiopian(text: str, language: Language) -> date | None:
    """Parse ``"<month> <day>, <year>"`` in ``language``, returning ``None`` if invalid.

    The accepted input is the display format above. Whitespace is flexible and the comma is
    optional, but the month name must belong to the named language and the day and year must
    form a real Ethiopian date. This is the parser the app's date field will use; a bad value
    is a validation failure for the caller to render, not an exception from this layer.
    """
    parts = text.replace(",", " ").split()
    if len(parts) != 3:
        return None

    month_name, day_text, year_text = parts
    try:
        day = int(day_text)
        year = int(year_text)
    except ValueError:
        return None

    try:
        month = MONTH_NAMES[language].index(month_name) + 1
    except ValueError:
        return None

    try:
        return from_ethiopian(EthiopianDate(year, month, day))
    except (OverflowError, ValueError):
        return None
