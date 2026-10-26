"""Which lane a message sends in, and when that lane goes quiet.

A text message is classified once, at build time, by the priority it declares in its
template file. Q1 is the lane a trader must never wait for — an authentication code, a
code that unlocks money — so it sends at any hour, unconditionally, with no knob to turn
it off. Q2 is the order-and-offer lane: important, but not worth waking someone for.
Q3 is the digest lane, read whenever the reader gets around to it. Q2 and Q3 wait out
the quiet window and go out the moment it closes.

The window is [21:00, 06:00) on the Ethiopia calendar day — Africa/Addis_Ababa, EAT, UTC+3,
which observes no daylight saving. What decides is the wall clock in that zone, never the
hour on the UTC timestamp: a message at 22:30 UTC is a message at 01:30 the next morning in
Addis, squarely inside the window, and a rule that compared the raw UTC hour would wrongly
let it out. A later slice computing *when* to drain the queue must honour the same calendar:
truncate the trigger timestamp to its EAT date (the semantic of ``date_trunc('day', ts AT
TIME ZONE 'Africa/Addis_Ababa')``), then add 06:00 EAT, then convert back to UTC. This
module only decides "send now" versus "queue it".
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from typing import Final

#: EAT is exactly UTC+3 with no daylight saving, so a fixed offset is the whole zone.
EAT_ZONE: Final[timezone] = timezone(timedelta(hours=3))

#: The quiet window is [21:00, 06:00) on the EAT calendar day.
QUIET_HOURS_START_EAT: Final[time] = time(21, 0)
QUIET_HOURS_END_EAT: Final[time] = time(6, 0)


class SmsPriority(StrEnum):
    """The three send lanes a template can declare for itself."""

    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"


#: The integer each lane is stored under in a queued row, so a drain orders by a number the
#: database sorts rather than by a name it would have to translate. Lower is higher priority:
#: a Q1 row is reached before any Q2 or Q3 row regardless of when it was written.
PRIORITY_RANK: Final[dict[SmsPriority, int]] = {
    SmsPriority.Q1: 1,
    SmsPriority.Q2: 2,
    SmsPriority.Q3: 3,
}

#: The inverse, so a stored rank is read back as the lane whose quiet-hour rule applies to it.
RANK_PRIORITY: Final[dict[int, SmsPriority]] = {rank: lane for lane, rank in PRIORITY_RANK.items()}


def priority_rank(priority: SmsPriority) -> int:
    """The integer a lane is stored under in a queued row."""
    return PRIORITY_RANK[priority]


def priority_from_rank(rank: int) -> SmsPriority:
    """The lane a stored rank names.

    Raises:
        ValueError: if the rank is not one of the three the vocabulary defines.
    """
    try:
        return RANK_PRIORITY[rank]
    except KeyError:
        raise ValueError(f"no send lane for rank {rank!r}") from None


def priority_for(template_id: str) -> SmsPriority:
    """The lane a template declared, read from the registry rather than from code here.

    Raises:
        KeyError: if the registry holds no template with this id.
    """
    # Imported lazily: the loader validates priorities through this module, so a
    # module-level import here would be an import cycle.
    from packages.shared.i18n.templates import load_templates

    try:
        return load_templates()[template_id].priority
    except KeyError:
        raise KeyError(f"no template with id {template_id!r} in the registry") from None


def eat_calendar_date(dt: datetime) -> date:
    """Truncate a timestamp to its EAT calendar day (Africa/Addis_Ababa, UTC+3).

    NFR-11: all date math uses EAT-zone truncation. The rule is applied by
    converting to EAT and taking the calendar date.
    """
    if dt.tzinfo is None:
        raise ValueError("dt must carry a timezone")

    return dt.astimezone(EAT_ZONE).date()


def should_queue_until_quiet_hours(send_at_utc: datetime, priority: SmsPriority) -> bool:
    """Whether this message waits for the quiet window to close before it is sent.

    True means "hold until quiet hours end"; False means "send now". Q1 never queues: a
    code somebody is waiting on right now may not wait for the morning.

    Raises:
        ValueError: if ``send_at_utc`` carries no timezone.
    """
    if send_at_utc.tzinfo is None:
        raise ValueError("send_at_utc must carry a timezone (pass an aware UTC datetime)")
    if priority is SmsPriority.Q1:
        return False
    on_eat_calendar = send_at_utc.astimezone(EAT_ZONE).replace(tzinfo=None)
    wall_clock = on_eat_calendar.time()
    return wall_clock >= QUIET_HOURS_START_EAT or wall_clock < QUIET_HOURS_END_EAT
