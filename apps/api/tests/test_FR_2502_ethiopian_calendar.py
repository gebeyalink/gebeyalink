"""FR-2502: the Ethiopian calendar conversion layer.

The API and storage stay UTC-only. This file exercises the conversion itself, without HTTP or
a database, against the two external anchors recorded in ADR 0017 rather than against values
the implementation happened to produce.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from packages.shared.ethiopian_calendar import (
    EPOCH,
    MONTH_NAMES,
    EthiopianDate,
    format_ethiopian,
    from_ethiopian,
    parse_ethiopian,
    to_ethiopian,
)
from packages.shared.i18n.languages import Language

pytestmark = pytest.mark.unit


def test_FR_2502_round_trip_over_three_consecutive_ethiopian_years() -> None:
    """A Gregorian date survives to-Ethiopian and back, including the leap year 2015."""
    start = date(2022, 9, 11)  # 1 Meskerem 2015
    dates = [start + timedelta(days=offset * 10) for offset in range(100)]

    assert len(dates) == 100
    assert {to_ethiopian(day).year for day in dates} == {2015, 2016, 2017}
    for gregorian in dates:
        assert from_ethiopian(to_ethiopian(gregorian)) == gregorian


def test_FR_2502_the_epoch_is_1_meskerem_1() -> None:
    """1 Meskerem 1 EA is 27 August 8 CE in the proleptic Gregorian calendar."""
    assert (
        to_ethiopian(EPOCH),
        from_ethiopian(EthiopianDate(1, 1, 1)),
    ) == (EthiopianDate(1, 1, 1), EPOCH)


def test_FR_2502_pagume_has_six_days_only_in_a_leap_year() -> None:
    """Y mod 4 == 3 is leap: 2015 and 2019 have Pagume 6, 2016-2018 do not."""
    for year in (2015, 2019):
        last_day = from_ethiopian(EthiopianDate(year, 13, 6))

        assert to_ethiopian(last_day) == EthiopianDate(year, 13, 6)
        assert to_ethiopian(last_day + timedelta(days=1)) == EthiopianDate(year + 1, 1, 1)

    for year in (2016, 2017, 2018):
        last_day = from_ethiopian(EthiopianDate(year, 13, 5))

        assert last_day + timedelta(days=1) == from_ethiopian(EthiopianDate(year + 1, 1, 1))
        with pytest.raises(ValueError):
            EthiopianDate(year, 13, 6)


def test_FR_2502_the_two_published_anchors_convert_correctly() -> None:
    """External references: 1 Meskerem 2015 = 11 Sep 2022, 1 Meskerem 2016 = 12 Sep 2023.

    These are the published anchor dates in ADR 0017. They are asserted here so a change to
    the arithmetic has to disagree with the outside world, not merely with itself.
    """
    assert from_ethiopian(EthiopianDate(2015, 1, 1)) == date(2022, 9, 11)
    assert from_ethiopian(EthiopianDate(2016, 1, 1)) == date(2023, 9, 12)


def test_FR_2502_each_language_formats_with_its_own_month_name() -> None:
    """1 Meskerem 2016 is rendered in Amharic, Oromo and English with the same number."""
    gregorian = date(2023, 9, 12)  # 1 Meskerem 2016

    formatted = {
        Language.AM: format_ethiopian(gregorian, Language.AM),
        Language.OM: format_ethiopian(gregorian, Language.OM),
        Language.EN: format_ethiopian(gregorian, Language.EN),
    }

    assert formatted[Language.AM].startswith("መስከረም")
    assert formatted[Language.OM].startswith("Meskerem")
    assert formatted[Language.EN].startswith("Meskerem")
    for text in formatted.values():
        assert text.endswith("1, 2016")


def test_FR_2502_parse_accepts_three_languages_and_refuses_bad_dates() -> None:
    """The display format parses back, and an impossible date returns None rather than raising."""
    valid = [
        ("መስከረም 1, 2016", Language.AM),
        ("Meskerem 1, 2016", Language.OM),
        ("Meskerem 1, 2016", Language.EN),
    ]
    invalid = [
        "Meskerem 32, 2016",
        "Pagume 6, 2016",  # 2016 is not a leap year
        "Meskerem 1, 2016 extra",
    ]

    for text, language in valid:
        assert parse_ethiopian(text, language) == date(2023, 9, 12)
    for text in invalid:
        assert parse_ethiopian(text, Language.EN) is None


def test_FR_2502_format_then_parse_returns_the_same_gregorian_date() -> None:
    """The display string is also a valid input string in the language that produced it."""
    for gregorian in (date(2023, 9, 12), date(2022, 9, 11), date(2023, 9, 10)):
        assert parse_ethiopian(format_ethiopian(gregorian, Language.AM), Language.AM) == gregorian


def test_FR_2502_all_thirteen_month_names_are_present() -> None:
    """Each language carries the whole year, including Pagume as the thirteenth month."""
    assert all(len(names) == len(MONTH_NAMES[Language.AM]) == 13 for names in MONTH_NAMES.values())
