"""Daily time-window helpers."""
from __future__ import annotations

import datetime as dt


def next_window(now: dt.datetime, start: dt.time, end: dt.time,
                prewarm_minutes: int = 0) -> tuple[dt.datetime, dt.datetime]:
    """Return (monitor_from, monitor_until) for the current or next window.

    `now` must be timezone-aware. If we are already inside today's window
    (including the prewarm lead time), monitor_from is in the past.
    """
    tz = now.tzinfo
    lead = dt.timedelta(minutes=prewarm_minutes)
    day = now.date()
    while True:
        open_at = dt.datetime.combine(day, start, tz) - lead
        close_at = dt.datetime.combine(day, end, tz)
        if now < close_at:
            return open_at, close_at
        day += dt.timedelta(days=1)
