"""FR-1004 — quiet hours: the lower lanes wait out the night, Q1 never does.

The window is [21:00, 06:00) on the EAT calendar day (Africa/Addis_Ababa, UTC+3, no
DST). 21:00 EAT is quiet; 06:00 EAT is not; 20:59 is not; 05:59 is. Q1 is exempt with no
configuration knob and no override. The EAT calendar day decides, never the raw UTC hour
of day: a timestamp at 22:30 UTC is 01:30 the next morning in Addis, inside the window,
and a rule that compared the UTC clock would wrongly send it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from packages.shared.messaging.queues import (
    EAT_ZONE,
    QUIET_HOURS_END_EAT,
    QUIET_HOURS_START_EAT,
    SmsPriority,
    eat_calendar_date,
    should_queue_until_quiet_hours,
)

pytestmark = pytest.mark.unit


def _utc(hour: int, minute: int = 0) -> datetime:
    """An aware UTC datetime on a fixed date; EAT is UTC+3, so its EAT hour is +3."""

    return datetime(2026, 10, 8, hour, minute, tzinfo=UTC)


def test_twenty_one_pm_eat_is_inside_the_quiet_window() -> None:
    # 21:00 EAT is 18:00 UTC on the same date: the window has opened.
    assert should_queue_until_quiet_hours(_utc(18, 0), SmsPriority.Q2) is True


def test_six_am_eat_is_outside_the_quiet_window() -> None:
    # 06:00 EAT is 03:00 UTC: the window has closed, so the message may go out.
    assert should_queue_until_quiet_hours(_utc(3, 0), SmsPriority.Q2) is False


def test_one_minute_before_the_window_is_not_quiet() -> None:
    # 20:59 EAT is 17:59 UTC.
    assert should_queue_until_quiet_hours(_utc(17, 59), SmsPriority.Q3) is False


def test_one_minute_inside_the_window_is_quiet() -> None:
    # 05:59 EAT is 02:59 UTC.
    assert should_queue_until_quiet_hours(_utc(2, 59), SmsPriority.Q3) is True


def test_q1_is_exempt_unconditionally_at_two_am_eat() -> None:
    # 02:00 EAT is 23:00 UTC the previous day — deepest quiet — and Q1 still sends.
    assert should_queue_until_quiet_hours(_utc(23, 0), SmsPriority.Q1) is False


@pytest.mark.parametrize(
    "utc_hour",
    [17, 18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6],
)
def test_q1_sends_at_every_hour_of_the_night(utc_hour: int) -> None:
    assert should_queue_until_quiet_hours(_utc(utc_hour), SmsPriority.Q1) is False


def test_the_eat_calendar_day_decides_not_the_utc_hour_of_day() -> None:
    # 22:30 UTC is 01:30 EAT the next morning: quiet wall clock in Addis, but a rule
    # reading the raw UTC hour would call it "before 21:00" and send it immediately.
    assert should_queue_until_quiet_hours(_utc(22, 30), SmsPriority.Q2) is True


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError):
        should_queue_until_quiet_hours(datetime(2026, 10, 8, 18, 0), SmsPriority.Q2)


def test_the_window_boundaries_are_the_declared_constants() -> None:
    assert time(21, 0) == QUIET_HOURS_START_EAT
    assert time(6, 0) == QUIET_HOURS_END_EAT


@pytest.mark.parametrize(
    ("utc_dt", "expected_eat_date"),
    [
        (datetime(2026, 10, 16, 20, 59, tzinfo=UTC), date(2026, 10, 16)),  # 23:59 EAT
        (datetime(2026, 10, 16, 21, 0, tzinfo=UTC), date(2026, 10, 17)),  # 00:00 EAT next day
        (datetime(2026, 10, 17, 0, 0, tzinfo=UTC), date(2026, 10, 17)),  # 03:00 EAT
        (datetime(2026, 10, 17, 20, 59, tzinfo=UTC), date(2026, 10, 17)),  # 23:59 EAT
    ],
)
def test_fr_2503_the_truncation_day_is_the_eat_calendar_day(
    utc_dt: datetime, expected_eat_date: date
) -> None:
    """FR-2503/NFR-11: truncation to the EAT calendar day, not the UTC day."""
    result = eat_calendar_date(utc_dt)
    assert isinstance(result, date)
    assert result == expected_eat_date

    # The inverted proof: for an instant whose UTC day is the previous day,
    # the EAT truncation is not the UTC truncation.
    crossing = datetime(2026, 10, 16, 21, 0, tzinfo=UTC)
    assert (
        eat_calendar_date(crossing) != crossing.date()
    )  # EAT day is 2026-10-17, UTC day is 2026-10-16
    assert eat_calendar_date(crossing) == date(2026, 10, 17)
    assert crossing.astimezone(EAT_ZONE).date() == date(2026, 10, 17)
