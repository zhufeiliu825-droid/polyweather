"""Local-day windowing. Bucketing observations by the city's LOCAL calendar date
(not the UTC date) is the single most important realized-max correctness rule:
a 23:30-local METAR belongs to today; getting the window wrong silently shifts
the whole day and corrupts every signal. ``zoneinfo`` handles BST/DST so the
window is defined purely by local calendar date.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo


def epoch_to_local(epoch_s: int, tz: str) -> datetime:
    """Convert a UTC epoch-seconds timestamp to a tz-aware local datetime."""
    return datetime.fromtimestamp(int(epoch_s), tz=ZoneInfo(tz))


def local_date_of(epoch_s: int, tz: str) -> date:
    """The local calendar date an observation belongs to."""
    return epoch_to_local(epoch_s, tz).date()


def in_local_day(epoch_s: int, tz: str, target: date) -> bool:
    """True iff the observation falls on ``target`` in the given timezone."""
    return local_date_of(epoch_s, tz) == target
