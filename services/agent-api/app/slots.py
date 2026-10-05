"""Scheduling rules for the booking assistant: pure functions, no Google calls, easy to test.

Slots are 30 minutes. A visitor may hold at most MAX_SLOTS_PER_VISITOR upcoming slots in total, used as separate
calls or as one longer meeting made of consecutive slots (30, 60 or 90 minutes).

The Google booking page's own availability cannot be read through the API, so its weekly pattern is mirrored here
(`Rules`). Anything blocked with ordinary calendar events is respected automatically through `busy`.
"""

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
SLOT = dt.timedelta(minutes=30)
MAX_SLOTS_PER_VISITOR = 3


@dataclass(frozen=True)
class Rules:
    work_start: int = 10                 # local hour
    work_end: int = 17
    days: tuple[int, ...] = (0, 1, 2, 3, 4)  # Monday..Friday
    # (weekday, from hour, to hour): recurring blocks mirrored from the Google schedule
    blocked: tuple[tuple[int, int, int], ...] = ((2, 13, 14), (4, 13, 14))  # Wednesday and Friday 13:00-14:00
    min_notice_h: int = 24
    horizon_days: int = 8
    buffer_min: int = 0


RULES = Rules()
Busy = list[tuple[dt.datetime, dt.datetime]]


def _blocked(t: dt.datetime, rules: Rules) -> bool:
    end = t + SLOT
    for weekday, h0, h1 in rules.blocked:
        if t.weekday() == weekday:
            b0 = t.replace(hour=h0, minute=0, second=0, microsecond=0)
            b1 = t.replace(hour=h1, minute=0, second=0, microsecond=0)
            if t < b1 and end > b0:
                return True
    return False


def free_slots(busy: Busy, now: dt.datetime, rules: Rules = RULES) -> list[dt.datetime]:
    """Start times of every free 30-minute slot inside the allowed window, in order."""
    earliest = now + dt.timedelta(hours=rules.min_notice_h)
    pad = dt.timedelta(minutes=rules.buffer_min)
    out: list[dt.datetime] = []
    day = earliest.date()
    last = (now + dt.timedelta(days=rules.horizon_days)).date()
    while day <= last:
        if day.weekday() in rules.days:
            t = dt.datetime(day.year, day.month, day.day, rules.work_start, tzinfo=TZ)
            stop = dt.datetime(day.year, day.month, day.day, rules.work_end, tzinfo=TZ)
            while t < stop:
                end = t + SLOT
                if (t >= earliest and not _blocked(t, rules)
                        and not any(t < b1 + pad and end > b0 - pad for b0, b1 in busy)):
                    out.append(t)
                t = end
        day += dt.timedelta(days=1)
    return out


def run_ok(free: set[dt.datetime], start: dt.datetime, n: int) -> bool:
    """True if n consecutive slots starting at `start` are all free (so a run never crosses a block or the day's end)."""
    return 1 <= n <= MAX_SLOTS_PER_VISITOR and all(start + SLOT * i in free for i in range(n))


def max_run(free: set[dt.datetime], start: dt.datetime, cap: int = MAX_SLOTS_PER_VISITOR) -> int:
    n = 0
    while n < cap and start + SLOT * n in free:
        n += 1
    return n


def label(start: dt.datetime, n: int = 1) -> str:
    """'Wed 07 Oct, 14:30 (Berlin time)' for one slot, 'Wed 07 Oct, 14:30-15:30 (Berlin time)' for a longer run."""
    if n == 1:
        return start.strftime("%a %d %b, %H:%M") + " (Berlin time)"
    end = start + SLOT * n
    return start.strftime("%a %d %b, %H:%M") + "-" + end.strftime("%H:%M") + " (Berlin time)"
